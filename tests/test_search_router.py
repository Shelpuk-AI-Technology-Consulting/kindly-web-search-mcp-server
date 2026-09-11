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
        os.environ.pop("SEARXNG_BASE_URL", None)
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

    async def test_uses_searxng_when_only_searxng_config(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ.pop("SERPER_API_KEY", None)
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

        with patch(
            "kindly_web_search_mcp_server.search.search_searxng", new_callable=AsyncMock
        ) as mock_searxng:
            mock_searxng.return_value = [
                WebSearchResult(title="X", link="https://example.com", snippet="S", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].title, "X")
        mock_searxng.assert_awaited()

    async def test_defaults_to_serper_when_both_keys(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"
        os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
            patch("kindly_web_search_mcp_server.search.search_searxng", new_callable=AsyncMock) as mock_searxng,
        ):
            mock_serper.return_value = [
                WebSearchResult(title="S", link="https://serper.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://serper.example")
        mock_serper.assert_awaited()
        mock_tavily.assert_not_awaited()
        mock_searxng.assert_not_awaited()

    async def test_uses_tavily_when_serper_unset_even_if_searxng_set(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ["TAVILY_API_KEY"] = "tvly_test"
        os.environ.pop("SERPER_API_KEY", None)
        os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

        with (
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
            patch("kindly_web_search_mcp_server.search.search_searxng", new_callable=AsyncMock) as mock_searxng,
        ):
            mock_tavily.return_value = [
                WebSearchResult(title="T", link="https://tavily.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://tavily.example")
        mock_tavily.assert_awaited()
        mock_searxng.assert_not_awaited()

    async def test_does_not_fallback_when_serper_errors(self) -> None:
        """A provider failure is surfaced, not retried against the next provider.

        The subject is unchanged: no second provider is tried. What changed is
        the type carried out of the router. It used to be the provider's raw
        ``httpx.HTTPStatusError``, whose message quotes the request URL -- which
        for SerpBase holds the API key, and which FastMCP renders into the error
        an MCP client receives. The router now converts that family into
        ``SearchProviderTransportError`` and keeps the original as ``__cause__``,
        so the assertion is retyped and the cause asserted alongside it rather
        than the check being dropped. See
        ``tests/test_provider_credential_disclosure.py``.
        """
        from kindly_web_search_mcp_server.search import (
            SearchProviderTransportError,
            search_web,
        )

        os.environ["SERPER_API_KEY"] = "serper_test"
        os.environ["TAVILY_API_KEY"] = "tvly_test"
        os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

        with (
            patch("kindly_web_search_mcp_server.search.search_serper", new_callable=AsyncMock) as mock_serper,
            patch("kindly_web_search_mcp_server.search.search_tavily", new_callable=AsyncMock) as mock_tavily,
            patch("kindly_web_search_mcp_server.search.search_searxng", new_callable=AsyncMock) as mock_searxng,
        ):
            mock_serper.side_effect = httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "https://google.serper.dev/search"),
                response=httpx.Response(401),
            )
            with self.assertRaises(SearchProviderTransportError) as raised:
                await search_web("q", num_results=1)

        # The raised type is now built by the router rather than handed up by the
        # provider, so on its own it no longer proves the provider was reached.
        # The await and the cause restore that coupling: the status is read from
        # the chained original, structurally, never out of a message.
        mock_serper.assert_awaited()
        self.assertIsInstance(raised.exception.__cause__, httpx.HTTPStatusError)
        self.assertEqual(raised.exception.__cause__.response.status_code, 401)

        mock_tavily.assert_not_awaited()
        mock_searxng.assert_not_awaited()

    async def test_uses_sofya_when_only_sofya_key(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        os.environ.pop("SERPER_API_KEY", None)
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ.pop("SEARXNG_BASE_URL", None)
        os.environ["SOFYA_API_KEY"] = "sofya_test"

        with patch(
            "kindly_web_search_mcp_server.search.search_sofya", new_callable=AsyncMock
        ) as mock_sofya:
            mock_sofya.return_value = [
                WebSearchResult(title="So", link="https://sofya.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://sofya.example")
        mock_sofya.assert_awaited()

    async def test_uses_youcom_when_only_youcom_key(self) -> None:
        from kindly_web_search_mcp_server.search import search_web

        for name in (
            "SERPER_API_KEY",
            "SERPBASE_API_KEY",
            "TAVILY_API_KEY",
            "SEARXNG_BASE_URL",
            "SOFYA_API_KEY",
        ):
            os.environ.pop(name, None)
        os.environ["YDC_API_KEY"] = "ydc_test"

        with patch(
            "kindly_web_search_mcp_server.search.search_youcom", new_callable=AsyncMock
        ) as mock_youcom:
            mock_youcom.return_value = [
                WebSearchResult(title="Y", link="https://youcom.example", snippet="sn", page_content="")
            ]
            out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://youcom.example")
        mock_youcom.assert_awaited()

    async def test_uses_serply_when_only_serply_key(self) -> None:
        """Select Serply when it is the only configured provider"""
        from kindly_web_search_mcp_server.search import PROVIDERS, search_web

        # `patch.dict` restores the environment, so no key leaks into later tests.
        with patch.dict(os.environ):
            for provider in PROVIDERS:
                os.environ.pop(provider.env_var, None)
            os.environ["SERPLY_API_KEY"] = "serply_test"

            with patch(
                "kindly_web_search_mcp_server.search.search_serply", new_callable=AsyncMock
            ) as mock_serply:
                mock_serply.return_value = [
                    WebSearchResult(title="S", link="https://serply.example", snippet="sn", page_content="")
                ]
                out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://serply.example")
        mock_serply.assert_awaited()

    async def test_prefers_youcom_over_serply_when_both_keys(self) -> None:
        """Keep an existing You.com deployment on You.com after Serply is added

        Serply is appended last, so setting its key beside any earlier provider's
        must not change which provider serves the query. You.com is the provider
        immediately before it in the registry.
        """
        from kindly_web_search_mcp_server.search import PROVIDERS, search_web

        with patch.dict(os.environ):
            for provider in PROVIDERS:
                os.environ.pop(provider.env_var, None)
            os.environ["YDC_API_KEY"] = "ydc_test"
            os.environ["SERPLY_API_KEY"] = "serply_test"

            with patch(
                "kindly_web_search_mcp_server.search.search_youcom", new_callable=AsyncMock
            ) as mock_youcom, patch(
                "kindly_web_search_mcp_server.search.search_serply", new_callable=AsyncMock
            ) as mock_serply:
                mock_youcom.return_value = [
                    WebSearchResult(title="Y", link="https://youcom.example", snippet="sn", page_content="")
                ]
                out = await search_web("q", num_results=1)

        self.assertEqual(out[0].link, "https://youcom.example")
        mock_serply.assert_not_awaited()

    async def test_raises_when_no_provider_configured(self) -> None:
        from kindly_web_search_mcp_server.search import WebSearchProviderError, search_web

        os.environ.pop("SERPER_API_KEY", None)
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ.pop("SEARXNG_BASE_URL", None)
        os.environ.pop("SOFYA_API_KEY", None)
        os.environ.pop("YDC_API_KEY", None)
        os.environ.pop("SERPLY_API_KEY", None)

        with self.assertRaises(WebSearchProviderError):
            await search_web("q", num_results=1)


if __name__ == "__main__":
    unittest.main()
