"""Cover credential redaction in the nodriver worker's launch-args diagnostics.

The worker emits ``worker.launch_args`` with the full Chromium command line. That
list includes ``--proxy-server`` built from ``KINDLY_CHROME_PROXY``, which may embed
credentials, so the emitted copy must be redacted while Chromium still receives the
real arguments.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape.nodriver_worker import (
    _build_chromium_launch_args,
)
from kindly_web_search_mcp_server.utils.diagnostics import redact_url_credentials

PROXY_WITH_CREDENTIALS = "http://alice:s3cr3t@proxy.corp:8080"


def _launch_args(monkeypatch, proxy: str) -> list[str]:
    """Build Chromium launch args with ``KINDLY_CHROME_PROXY`` set to ``proxy``.

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        proxy: Value to expose as ``KINDLY_CHROME_PROXY``.

    Returns:
        The generated Chromium command-line arguments.
    """
    monkeypatch.setenv("KINDLY_CHROME_PROXY", proxy)
    return _build_chromium_launch_args(
        base_browser_args=[],
        user_data_dir="/tmp/profile",
        user_agent="UA",
        host="127.0.0.1",
        port=9222,
        sandbox_enabled=False,
    )


def test_chromium_receives_the_real_proxy_credentials(monkeypatch) -> None:
    """Keep credentials intact in the args actually passed to Chromium"""
    args = _launch_args(monkeypatch, PROXY_WITH_CREDENTIALS)

    assert f"--proxy-server={PROXY_WITH_CREDENTIALS}" in args


def test_emitted_launch_args_hide_proxy_credentials(monkeypatch) -> None:
    """Redact credentials from the copy emitted to diagnostics"""
    args = _launch_args(monkeypatch, PROXY_WITH_CREDENTIALS)

    emitted = [redact_url_credentials(arg) for arg in args]

    assert "--proxy-server=http://***@proxy.corp:8080" in emitted
    assert not any("s3cr3t" in arg for arg in emitted)
    assert not any("alice" in arg for arg in emitted)


def test_emitted_launch_args_keep_proxy_host_visible(monkeypatch) -> None:
    """Preserve the proxy host and port so the diagnostic stays useful"""
    args = _launch_args(monkeypatch, PROXY_WITH_CREDENTIALS)

    emitted = [redact_url_credentials(arg) for arg in args]

    assert any("proxy.corp:8080" in arg for arg in emitted)


def test_credential_free_args_are_unchanged(monkeypatch) -> None:
    """Leave ordinary arguments untouched"""
    args = _launch_args(monkeypatch, "http://proxy.corp:8080")

    assert [redact_url_credentials(arg) for arg in args] == args
