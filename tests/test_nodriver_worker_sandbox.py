from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch


class TestNodriverWorkerSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_disables_sandbox_by_default(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        class _FakePage:
            def get_content(self):
                return "<html><body>ok</body></html>"

            async def close(self):
                return None

        class _FakeBrowser:
            async def get(self, _url: str):
                return _FakePage()

            async def stop(self):
                return None

        fake_start = AsyncMock(return_value=_FakeBrowser())

        with patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}), patch.dict(
            "os.environ", {}, clear=False
        ):
            html = await _fetch_html(
                "https://example.com",
                referer=None,
                user_agent="ua",
                wait_seconds=0.0,
                browser_executable_path=None,
            )

        self.assertIn("ok", html)
        _, kwargs = fake_start.call_args
        self.assertIn("sandbox", kwargs)
        self.assertFalse(kwargs["sandbox"])

    async def test_allows_enabling_sandbox_via_env(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        class _FakePage:
            def get_content(self):
                return "<html><body>ok</body></html>"

            async def close(self):
                return None

        class _FakeBrowser:
            async def get(self, _url: str):
                return _FakePage()

            async def stop(self):
                return None

        fake_start = AsyncMock(return_value=_FakeBrowser())

        with patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}), patch.dict(
            "os.environ", {"KINDLY_NODRIVER_SANDBOX": "1"}, clear=False
        ):
            await _fetch_html(
                "https://example.com",
                referer=None,
                user_agent="ua",
                wait_seconds=0.0,
                browser_executable_path=None,
            )

        _, kwargs = fake_start.call_args
        self.assertTrue(kwargs["sandbox"])

    async def test_forces_sandbox_off_when_running_as_root(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        class _FakePage:
            def get_content(self):
                return "<html><body>ok</body></html>"

            async def close(self):
                return None

        class _FakeBrowser:
            async def get(self, _url: str):
                return _FakePage()

            async def stop(self):
                return None

        fake_start = AsyncMock(return_value=_FakeBrowser())

        with (
            patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}),
            patch.dict("os.environ", {"KINDLY_NODRIVER_SANDBOX": "1"}, clear=False),
            patch("os.geteuid", return_value=0),
            patch("shutil.which", return_value="/usr/bin/chromium"),
        ):
            await _fetch_html(
                "https://example.com",
                referer=None,
                user_agent="ua",
                wait_seconds=0.0,
                browser_executable_path=None,
            )

        _, kwargs = fake_start.call_args
        self.assertFalse(kwargs["sandbox"])
        self.assertIn("--no-sandbox", kwargs.get("browser_args", []))

    async def test_resolves_browser_executable_from_path(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        class _FakePage:
            def get_content(self):
                return "<html><body>ok</body></html>"

            async def close(self):
                return None

        class _FakeBrowser:
            async def get(self, _url: str):
                return _FakePage()

            async def stop(self):
                return None

        fake_start = AsyncMock(return_value=_FakeBrowser())

        with (
            patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}),
            patch.dict("os.environ", {}, clear=False),
            patch("shutil.which", return_value="/usr/bin/chromium"),
        ):
            await _fetch_html(
                "https://example.com",
                referer=None,
                user_agent="ua",
                wait_seconds=0.0,
                browser_executable_path=None,
            )

        _, kwargs = fake_start.call_args
        self.assertEqual(kwargs.get("browser_executable_path"), "/usr/bin/chromium")

    async def test_errors_when_no_browser_found(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        fake_start = AsyncMock()

        with (
            patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}),
            patch.dict("os.environ", {}, clear=False),
            patch("shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "KINDLY_BROWSER_EXECUTABLE_PATH"):
                await _fetch_html(
                    "https://example.com",
                    referer=None,
                    user_agent="ua",
                    wait_seconds=0.0,
                    browser_executable_path=None,
                )

    async def test_retries_on_failed_to_connect_to_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.nodriver_worker import _fetch_html

        class _FakePage:
            def get_content(self):
                return "<html><body>ok</body></html>"

            async def close(self):
                return None

        class _FakeBrowser:
            async def get(self, _url: str):
                return _FakePage()

            async def stop(self):
                return None

        fake_start = AsyncMock(side_effect=[RuntimeError("Failed to connect to browser"), _FakeBrowser()])

        with (
            patch.dict("sys.modules", {"nodriver": type("X", (), {"start": fake_start})}),
            patch.dict("os.environ", {"KINDLY_NODRIVER_RETRY_ATTEMPTS": "2"}, clear=False),
            patch("shutil.which", return_value="/snap/bin/chromium"),
        ):
            html = await _fetch_html(
                "https://example.com",
                referer=None,
                user_agent="ua",
                wait_seconds=0.0,
                browser_executable_path=None,
            )

        self.assertIn("ok", html)
        self.assertEqual(fake_start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
