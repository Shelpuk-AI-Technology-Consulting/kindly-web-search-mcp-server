from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult


class TestWebSearchTool(unittest.IsolatedAsyncioTestCase):
    async def test_web_search_returns_results(self) -> None:
        from kindly_web_search_mcp_server.server import web_search

        mocked_results = [
            WebSearchResult(title="T", link="https://example.com", snippet="S", page_content=None)
        ]

        with patch(
            "kindly_web_search_mcp_server.server.search_web", new_callable=AsyncMock
        ) as mock_search:
            mock_search.return_value = mocked_results

            out = await web_search("hello", num_results=1, return_full_pages=False)

        self.assertIsInstance(out, dict)
        self.assertIn("results", out)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["title"], "T")
        self.assertEqual(out["results"][0]["link"], "https://example.com")
        self.assertEqual(out["results"][0]["snippet"], "S")
        self.assertIsNone(out["results"][0].get("page_content"))

    async def test_get_content_returns_markdown(self) -> None:
        from kindly_web_search_mcp_server.server import get_content

        with patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.return_value = "# Title\n\nHello"
            out = await get_content("https://example.com")

        self.assertEqual(out["url"], "https://example.com")
        self.assertIn("page_content", out)
        self.assertIn("Hello", out["page_content"])

    async def test_get_content_handles_none(self) -> None:
        from kindly_web_search_mcp_server.server import get_content

        with patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.return_value = None
            out = await get_content("https://example.com/file.pdf")

        self.assertEqual(out["url"], "https://example.com/file.pdf")
        self.assertIn("Could not retrieve content", out["page_content"])


if __name__ == "__main__":
    unittest.main()
