from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestUniversalHtmlLoader(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_url_returns_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import load_url_as_markdown

        out = await load_url_as_markdown("https://example.com/file.pdf")
        self.assertIsNone(out)

    async def test_converts_html_to_markdown(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import load_url_as_markdown

        html = "<html><body><main><h1>Title</h1><p>Hello world</p></main></body></html>"

        with patch(
            "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = html
            out = await load_url_as_markdown("https://example.com")

        self.assertIsInstance(out, str)
        self.assertIn("Title", out)
        self.assertIn("Hello world", out)

    async def test_fetch_html_spawns_worker_subprocess(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import fetch_html_via_nodriver

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"<html><body><p>ok</p></body></html>", b"noisy but ignored"

        with patch(
            "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as mock_spawn:
            mock_spawn.return_value = _FakeProc()
            html = await fetch_html_via_nodriver("https://example.com")

        self.assertIn("ok", html)
        self.assertTrue(mock_spawn.called)
        args, kwargs = mock_spawn.call_args
        self.assertIn("-m", args)
        self.assertIn("kindly_web_search_mcp_server.scrape.nodriver_worker", args)
        self.assertIn("env", kwargs)
        self.assertIn("PYTHONPATH", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
