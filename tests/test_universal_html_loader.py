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

    async def test_default_total_timeout_is_60(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
        )

        config = UniversalHtmlLoaderConfig()
        self.assertEqual(config.total_timeout_seconds, 60.0)

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

    async def test_fetch_html_passes_browser_executable_path_when_set(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import fetch_html_via_nodriver

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"<html><body><p>ok</p></body></html>", b""

        with patch.dict("os.environ", {"KINDLY_BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium"}), patch(
            "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as mock_spawn:
            mock_spawn.return_value = _FakeProc()
            await fetch_html_via_nodriver("https://example.com")

        args, _kwargs = mock_spawn.call_args
        self.assertIn("--browser-executable-path", args)
        self.assertIn("/usr/bin/chromium", args)

    async def test_fetch_html_sets_no_proxy_for_loopback(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import fetch_html_via_nodriver

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"<html><body><p>ok</p></body></html>", b""

        with patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://proxy.invalid:8080"},
            clear=False,
        ), patch(
            "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as mock_spawn:
            mock_spawn.return_value = _FakeProc()
            await fetch_html_via_nodriver("https://example.com")

        _args, kwargs = mock_spawn.call_args
        env = kwargs.get("env") or {}
        no_proxy = (env.get("NO_PROXY") or env.get("no_proxy") or "").lower()
        self.assertIn("localhost", no_proxy)
        self.assertIn("127.0.0.1", no_proxy)


if __name__ == "__main__":
    unittest.main()
