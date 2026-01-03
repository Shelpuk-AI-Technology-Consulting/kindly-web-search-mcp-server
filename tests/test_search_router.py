from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from kindly_web_search_mcp_server.models import WebSearchResult


class TestSearchRouter(unittest.IsolatedAsyncioTestCase):
    async def test_uses_tavily_when_only_tavily_key(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ.pop("SERPER_API_KEY", None)
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with patch(
            "kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock
        ) as mock_tavily:
            mock_tavily.return_value = [
                WebSearchResult(title="T", link="https://example.com", snippet="S", page_content="")
            ]

            out = await search_web("q", num_results=1)

        self.assertEqual(len(out), 1)
        mock_tavily.assert_awaited()

    async def test_defaults_to_serper_when_both_keys(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
        ):
            mock_serper.return_value = [
                WebSearchResult(title="S", link="https://serper.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://serper.example")
        mock_serper.assert_awaited()
        mock_tavily.assert_not_awaited()

    async def test_falls_back_to_tavily_when_serper_fails(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
        ):
            mock_serper.side_effect = httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", "https://google.serper.dev/search"), response=httpx.Response(500)
            )
            mock_tavily.return_value = [
                WebSearchResult(title="T", link="https://tavily.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://tavily.example")
        mock_tavily.assert_awaited()

    async def test_does_not_fallback_on_auth_failure(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
        ):
            mock_serper.side_effect = httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "https://google.serper.dev/search"),
                response=httpx.Response(401),
            )
            with self.assertRaises(httpx.HTTPStatusError):
                await search_web("q", num_results=1)

        mock_tavily.assert_not_awaited()

    async def test_fallback_on_rate_limit(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
        ):
            mock_serper.side_effect = httpx.HTTPStatusError(
                "rate-limited",
                request=httpx.Request("POST", "https://google.serper.dev/search"),
                response=httpx.Response(429),
            )
            mock_tavily.return_value = [
                WebSearchResult(title="T", link="https://tavily.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://tavily.example")
        mock_tavily.assert_awaited()

    async def test_falls_back_on_provider_error(self) -> None:
        from kindly_web_search_mcp_server.search import search_web
        from kindly_web_search_mcp_server.search.serper import SerperError

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
        ):
            mock_serper.side_effect = SerperError("bad JSON")
            mock_tavily.return_value = [
                WebSearchResult(title="T", link="https://tavily.example", snippet="sn", page_content="")
            ]

            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://tavily.example")
        mock_tavily.assert_awaited()


if __name__ == "__main__":
    unittest.main()
