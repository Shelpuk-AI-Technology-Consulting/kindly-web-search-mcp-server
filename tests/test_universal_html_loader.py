from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.doubles.worker_process import FakeWorkerProcess, primed_reader

#: Stdout the worker double hands back. Fed through a real `asyncio.StreamReader`,
#: so the bytes travel the path production reads: this is the wiring that broke,
#: and asserting the returned value is what proves it intact.
WORKER_STDOUT = b"<html><body><p>ok</p></body></html>"

#: Every variable `fetch_html_via_nodriver` reads that can change what these
#: tests observe. Cleared for each case, which then declares what it needs.
#:
#: **This is a superset, deliberately.** Some entries are read only on branches
#: these three never take; clearing one costs nothing and missing one costs a
#: test that passes while asserting nothing.
#:
#: Two are not hypothetical. Measured with the production behaviour fully
#: removed:
#:
#: * `PYTHONPATH` — the spawn case asserts the key is in the child environment,
#:   which is `dict(os.environ)`. Exported ambiently it satisfies that assertion
#:   with `_maybe_add_src_to_pythonpath` reduced to a no-op: **fails on a clean
#:   env, passes with `PYTHONPATH` set.**
#: * `NO_PROXY` / `no_proxy` — likewise for the loopback case with
#:   `_ensure_no_proxy_localhost_env` reduced to a no-op: **fails clean, passes
#:   with `NO_PROXY=localhost,127.0.0.1` set.**
#:
#: Both were found by review, not by the author's falsification pass, which ran
#: on a shell exporting neither. A control that only fires on one machine is no
#: control — the lesson `test_nodriver_worker_sandbox.py` records for `CHROME_BIN`.
READ_ENVIRONMENT_VARIABLES = (
    "KINDLY_NODRIVER_REUSE_BROWSER",
    "KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST",
    "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS",
    "KINDLY_DIAGNOSTICS",
    "KINDLY_REQUEST_ID",
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "PYTHONPATH",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


@contextlib.contextmanager
def pinned_environment(**overrides: str) -> Iterator[None]:
    """Run a case with every variable the loader reads cleared, then declared.

    `KINDLY_NODRIVER_REUSE_BROWSER` is forced off rather than merely cleared.
    `reuse_enabled()` returns True unless explicitly disabled, so without it
    `fetch_html_via_nodriver` enters the Chromium pool and reaches the spawn
    under test only because `pool.acquire` raises and is swallowed. Measured
    before this pin: `get_chromium_pool` entered once per test, leaving a
    module-global pool and real browser trees behind. The pooled path is a
    subsystem concern and is not this file's to cover.

    Args:
        **overrides: Variables this case needs set, applied after the clear.

    Yields:
        None, for the duration of the patched environment.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in READ_ENVIRONMENT_VARIABLES
    }
    environment["KINDLY_NODRIVER_REUSE_BROWSER"] = "0"
    environment.update(overrides)
    with patch.dict("os.environ", environment, clear=True):
        yield


class TestUniversalHtmlLoader(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_url_returns_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        out = await load_url_as_markdown("https://example.com/file.pdf")
        self.assertIsNone(out)

    async def test_default_total_timeout_is_60(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
        )

        config = UniversalHtmlLoaderConfig()
        self.assertEqual(config.total_timeout_seconds, 60.0)

    async def test_converts_html_to_markdown(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

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
        """Spawn the worker module, and return what its stdout produced"""
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            mock_spawn.return_value = FakeWorkerProcess(
                stdout=primed_reader(WORKER_STDOUT),
                stderr=primed_reader(b"noisy but ignored"),
            )
            html = await fetch_html_via_nodriver("https://example.com")

        # The returned markup is the assertion that matters: it is the only one
        # that fails if the parent stops reading the child's stdout, the drift
        # that left this test red. Compared whole rather than by substring, so
        # truncation or mangling through the accumulator's decode fails too.
        self.assertEqual(html, WORKER_STDOUT.decode())
        self.assertTrue(mock_spawn.called)
        args, kwargs = mock_spawn.call_args
        # Membership, not adjacency: the builder's exact shape is asserted at the
        # unit layer once `_build_worker_command` is extracted. Pinning order
        # here as well would put one claim at two layers.
        self.assertIn("-m", args)
        self.assertIn("kindly_web_search_mcp_server.scrape.nodriver_worker", args)
        self.assertIn("env", kwargs)
        self.assertIn("PYTHONPATH", kwargs["env"])

    async def test_fetch_html_passes_browser_executable_path_when_set(self) -> None:
        """Forward the configured browser path to the worker command line"""
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(KINDLY_BROWSER_EXECUTABLE_PATH="/usr/bin/chromium"),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            mock_spawn.return_value = FakeWorkerProcess(
                stdout=primed_reader(WORKER_STDOUT), stderr=primed_reader(b"")
            )
            await fetch_html_via_nodriver("https://example.com")

        args, _kwargs = mock_spawn.call_args
        self.assertIn("--browser-executable-path", args)
        self.assertIn("/usr/bin/chromium", args)

    async def test_fetch_html_sets_no_proxy_for_loopback(self) -> None:
        """Exempt loopback from the proxy, so the child can reach its own DevTools"""
        from kindly_web_search_mcp_server.scrape.universal_html import (
            fetch_html_via_nodriver,
        )

        with (
            pinned_environment(
                HTTP_PROXY="http://proxy.invalid:8080",
                # Declared on: this case's whole subject is the behaviour the
                # variable gates, so an ambient "0" would leave it passing for
                # having asserted nothing.
                KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST="1",
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            mock_spawn.return_value = FakeWorkerProcess(
                stdout=primed_reader(WORKER_STDOUT), stderr=primed_reader(b"")
            )
            await fetch_html_via_nodriver("https://example.com")

        _args, kwargs = mock_spawn.call_args
        env = kwargs.get("env") or {}
        no_proxy = (env.get("NO_PROXY") or env.get("no_proxy") or "").lower()
        self.assertIn("localhost", no_proxy)
        self.assertIn("127.0.0.1", no_proxy)


class TestMarkdownSuffixProbe(unittest.IsolatedAsyncioTestCase):
    """markdown-suffix fast path: wiring, rewrite, allowlist gate, cap, errors."""

    async def test_md_suffix_hit_returns_markdown_without_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        md = "# Title\n\nRendered body from the .md endpoint.\n"
        with (
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_suffix",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_probe.return_value = md
            out = await load_url_as_markdown(
                "https://help.aliyun.com/zh/oss/user-guide/policy"
            )

        self.assertEqual(out, md)
        mock_nodriver.assert_not_called()

    async def test_md_suffix_miss_falls_through_to_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        html = "<html><body><main><h1>Real</h1><p>content</p></main></body></html>"
        with (
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_suffix",
                new_callable=AsyncMock,
            ) as mock_probe,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_probe.return_value = None
            mock_nodriver.return_value = html
            out = await load_url_as_markdown(
                "https://help.aliyun.com/zh/oss/user-guide/policy"
            )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Real", out)
        mock_nodriver.assert_called_once()

    def test_build_md_suffix_url_rewrite_cases(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            _build_md_suffix_url,
        )

        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p"),
            "https://help.aliyun.com/zh/oss/p.md",
        )
        # query is preserved and .md lands before it
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p?spm=1"),
            "https://help.aliyun.com/zh/oss/p.md?spm=1",
        )
        # fragment is preserved
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p#sec"),
            "https://help.aliyun.com/zh/oss/p.md#sec",
        )
        # already .md is idempotent
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/zh/oss/p.md"),
            "https://help.aliyun.com/zh/oss/p.md",
        )
        # .html -> .md (path segment preserved, not stripped)
        self.assertEqual(
            _build_md_suffix_url("https://help.aliyun.com/document_detail/123.html"),
            "https://help.aliyun.com/document_detail/123.md",
        )
        # trailing slash is not a doc leaf -> None
        self.assertIsNone(_build_md_suffix_url("https://help.aliyun.com/zh/oss/"))

    async def test_non_allowlisted_host_skips_probe_no_diagnostic(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        diag = Diagnostics(request_id="t", enabled=True)
        # example.com is not in the allowlist -> probe skips silently (no httpx, no emit)
        with patch.dict(
            "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
        ):
            result = await _probe_markdown_suffix(
                "https://example.com/page",
                config=UniversalHtmlLoaderConfig(),
                diagnostics=diag,
            )

        self.assertIsNone(result)
        self.assertFalse(
            any(e["stage"] == "content.md_suffix_probe" for e in diag.entries)
        )

    async def test_md_suffix_probe_caps_overlong_markdown(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )

        big_body = ("x" * 60_000).encode("utf-8")

        class _FakeResp:
            status_code = 200
            headers = {"content-type": "text/markdown; charset=utf-8"}
            content = big_body

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                return _FakeResp()

        with (
            patch.dict(
                "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            out = await _probe_markdown_suffix(
                "https://help.aliyun.com/zh/oss/p",
                config=UniversalHtmlLoaderConfig(),
            )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("…(truncated)", out)
        self.assertLessEqual(
            len(out), UniversalHtmlLoaderConfig().max_markdown_chars + 64
        )

    async def test_md_suffix_probe_swallows_httpx_errors(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                raise RuntimeError("network down")

        with (
            patch.dict(
                "os.environ", {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"}
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            out = await _probe_markdown_suffix(
                "https://help.aliyun.com/zh/oss/p",
                config=UniversalHtmlLoaderConfig(),
            )

        # never raises into the caller; None -> caller falls back to the browser
        self.assertIsNone(out)

    async def test_md_suffix_probe_rejects_invalid_responses(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_suffix,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        class _FakeResp:
            def __init__(self, status_code, content_type, body):
                self.status_code = status_code
                self.headers = {"content-type": content_type}
                self.content = body

        # each case must fail the four-way gate and return None with a
        # validation_failed miss diagnostic (the self-verifying degradation)
        cases = [
            ("non-200", 404, "text/markdown", b"x" * 2048),
            ("wrong content-type", 200, "text/html", b"x" * 2048),
            ("body under floor", 200, "text/markdown", b"x" * 100),
        ]
        for name, status, ctype, body in cases:
            with self.subTest(name):

                class _FakeClient:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_exc):
                        return False

                    async def get(self, *_args, **_kwargs):
                        return _FakeResp(status, ctype, body)

                diag = Diagnostics(request_id="t", enabled=True)
                with (
                    patch.dict(
                        "os.environ",
                        {"KINDLY_MARKDOWN_SUFFIX_HOSTS": "help.aliyun.com"},
                    ),
                    patch(
                        "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                        return_value=_FakeClient(),
                    ),
                ):
                    out = await _probe_markdown_suffix(
                        "https://help.aliyun.com/zh/oss/p",
                        config=UniversalHtmlLoaderConfig(),
                        diagnostics=diag,
                    )

            self.assertIsNone(out, f"{name}: expected a miss")
            self.assertTrue(
                any(
                    e["stage"] == "content.md_suffix_probe"
                    and e["data"].get("result") == "miss"
                    and e["data"].get("reason") == "validation_failed"
                    for e in diag.entries
                ),
                f"{name}: expected a validation_failed miss diagnostic",
            )


class TestMarkdownAcceptBlanketProbe(unittest.IsolatedAsyncioTestCase):
    """Blanket Accept: text/markdown probe (opt-in, double-fetch on text/html)."""

    async def test_switch_off_does_not_call_blanket_probe(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        html = "<html><body><main><p>browser content</p></main></body></html>"
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "0"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_accept_blanket",
                new_callable=AsyncMock,
            ) as mock_blanket,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_nodriver.return_value = html
            out = await load_url_as_markdown("https://example.com/page")

        mock_blanket.assert_not_called()
        mock_nodriver.assert_called_once()
        self.assertIsNotNone(out)

    async def test_switch_on_hit_skips_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        md = "# Negotiated\n\nBody from the .md-by-Accept endpoint.\n"
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "1"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html._probe_markdown_accept_blanket",
                new_callable=AsyncMock,
            ) as mock_blanket,
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_blanket.return_value = md
            out = await load_url_as_markdown("https://example.com/page")

        self.assertEqual(out, md)
        mock_nodriver.assert_not_called()

    async def test_text_html_falls_through_to_browser(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            load_url_as_markdown,
        )

        # the blanket probe (real) GETs and gets text/html -> miss; browser re-fetches
        class _FakeResp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"x" * 4096

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, *_args, **_kwargs):
                return _FakeResp()

        html = (
            "<html><body><main><h1>Rendered</h1><p>via browser</p></main></body></html>"
        )
        with (
            patch.dict("os.environ", {"KINDLY_MARKDOWN_ACCEPT_PROBE": "1"}),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
            patch(
                "kindly_web_search_mcp_server.scrape.universal_html.fetch_html_via_nodriver",
                new_callable=AsyncMock,
            ) as mock_nodriver,
        ):
            mock_nodriver.return_value = html
            out = await load_url_as_markdown("https://example.com/page")

        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("Rendered", out)
        mock_nodriver.assert_called_once()

    async def test_validation_failures_return_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_accept_blanket,
        )
        from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

        class _FakeResp:
            def __init__(self, status_code, content_type, body):
                self.status_code = status_code
                self.headers = {"content-type": content_type}
                self.content = body

        cases = [
            ("server returns html", 200, "text/html", b"x" * 2048),
            ("non-200", 404, "text/markdown", b"x" * 2048),
            ("body under floor", 200, "text/markdown", b"x" * 100),
        ]
        for name, status, ctype, body in cases:
            with self.subTest(name):

                class _FakeClient:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_exc):
                        return False

                    async def get(self, *_args, **_kwargs):
                        return _FakeResp(status, ctype, body)

                diag = Diagnostics(request_id="t", enabled=True)
                with patch(
                    "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
                    return_value=_FakeClient(),
                ):
                    out = await _probe_markdown_accept_blanket(
                        "https://example.com/page",
                        config=UniversalHtmlLoaderConfig(),
                        diagnostics=diag,
                    )

            self.assertIsNone(out, f"{name}: expected a miss")
            self.assertTrue(
                any(
                    e["stage"] == "content.md_accept_probe"
                    and e["data"].get("result") == "miss"
                    and e["data"].get("reason") == "validation_failed"
                    for e in diag.entries
                ),
                f"{name}: expected a validation_failed miss diagnostic",
            )

    async def test_httpx_error_returns_none(self) -> None:
        from kindly_web_search_mcp_server.scrape.universal_html import (
            UniversalHtmlLoaderConfig,
            _probe_markdown_accept_blanket,
        )

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        with patch(
            "kindly_web_search_mcp_server.scrape.universal_html.httpx.AsyncClient",
            return_value=_FakeClient(),
        ):
            out = await _probe_markdown_accept_blanket(
                "https://example.com/page",
                config=UniversalHtmlLoaderConfig(),
            )

        # never raises into the caller; None -> caller falls back to the browser
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
