"""Cover transport selection and transport security for the HTTP/SSE entry points.

These are the behaviours that have no other guard: ``main`` never returns for a live
server, so the ASGI app is captured by patching :func:`uvicorn.run` and inspected
instead of being served. The origin tests check the CORS policy against the SDK's own
validator rather than against a hand-written expectation, because the defect being
guarded is the two disagreeing, not either one in isolation.
"""

from __future__ import annotations

import logging
import re

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware

from kindly_web_search_mcp_server.server import (
    LOCALHOST_ALLOWED_HOSTS,
    LOCALHOST_ALLOWED_ORIGINS,
    _cors_origin_regex,
    _resolve_transport,
    _resolve_transport_security,
    _split_env_list,
)


# --- transport resolution -----------------------------------------------------------


def test_transport_defaults_to_stdio_when_nothing_is_set(monkeypatch) -> None:
    """Fall back to stdio when neither the flag nor the environment selects one"""
    monkeypatch.delenv("FASTMCP_TRANSPORT", raising=False)
    assert _resolve_transport(None) == "stdio"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("stdio", "stdio"),
        ("sse", "sse"),
        ("streamable-http", "streamable-http"),
        ("http", "streamable-http"),
        ("  http  ", "streamable-http"),
    ],
)
def test_transport_env_var_is_honoured(monkeypatch, env_value: str, expected: str) -> None:
    """Honour ``FASTMCP_TRANSPORT``, normalising the ``http`` alias"""
    monkeypatch.setenv("FASTMCP_TRANSPORT", env_value)
    assert _resolve_transport(None) == expected


def test_cli_flag_wins_over_env_var(monkeypatch) -> None:
    """Prefer the explicit CLI flag over ambient environment"""
    monkeypatch.setenv("FASTMCP_TRANSPORT", "streamable-http")
    assert _resolve_transport("sse") == "sse"


def test_blank_env_var_is_treated_as_unset(monkeypatch, caplog) -> None:
    """Treat a blank ``FASTMCP_TRANSPORT`` as unset, without warning"""
    monkeypatch.setenv("FASTMCP_TRANSPORT", "   ")
    with caplog.at_level(logging.WARNING):
        assert _resolve_transport(None) == "stdio"
    assert caplog.records == []


def test_invalid_env_var_falls_back_to_stdio_with_a_warning(monkeypatch, caplog) -> None:
    """Warn before falling back, so a Compose typo is not silent"""
    monkeypatch.setenv("FASTMCP_TRANSPORT", "htpp")
    with caplog.at_level(logging.WARNING):
        assert _resolve_transport(None) == "stdio"
    assert any("htpp" in record.getMessage() for record in caplog.records)


def test_http_is_accepted_by_the_transport_flag() -> None:
    """Accept ``--transport http`` because the env var accepts ``http``"""
    from kindly_web_search_mcp_server.server import _build_arg_parser

    args = _build_arg_parser().parse_args(["--transport", "http"])
    assert _resolve_transport(args.transport) == "streamable-http"


# --- transport security defaults ----------------------------------------------------


def test_split_env_list_trims_and_drops_blanks() -> None:
    """Trim entries and drop the empties a trailing comma leaves behind"""
    assert _split_env_list(" a:* , ,b:8000 ") == ["a:*", "b:8000"]
    assert _split_env_list(None) == []
    assert _split_env_list("") == []


def test_unconfigured_server_keeps_loopback_protection() -> None:
    """Keep DNS rebinding protection on when no allowlist is configured

    This is the regression that matters most: an empty allowlist previously turned
    protection off entirely for every user who had not set the variables.
    """
    settings, cors_origins = _resolve_transport_security([], [])

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == list(LOCALHOST_ALLOWED_HOSTS)
    assert settings.allowed_origins == list(LOCALHOST_ALLOWED_ORIGINS)
    assert cors_origins == list(LOCALHOST_ALLOWED_ORIGINS)


def test_configuring_hosts_alone_keeps_loopback_origins() -> None:
    """Leave origins working when only ``FASTMCP_ALLOWED_HOSTS`` is set

    Setting the hosts alone is the natural reaction to a 421 behind Compose. It must
    not leave an empty origin allowlist, which rejects every browser request with 403.
    """
    settings, cors_origins = _resolve_transport_security(["kindly-web-search-mcp:*"], [])

    assert settings.allowed_hosts == ["kindly-web-search-mcp:*"]
    assert settings.allowed_origins == list(LOCALHOST_ALLOWED_ORIGINS)
    assert cors_origins == list(LOCALHOST_ALLOWED_ORIGINS)


def test_configured_origins_are_used_verbatim() -> None:
    """Use the configured origins for both the SDK and the CORS policy"""
    settings, cors_origins = _resolve_transport_security([], ["https://app.example"])

    assert settings.allowed_origins == ["https://app.example"]
    assert cors_origins == settings.allowed_origins


# --- CORS / transport-security agreement --------------------------------------------

SAMPLE_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
    "https://evil.example",
    "http://localhost.evil.example",
    "https://localhost:3000",
)


@pytest.mark.parametrize("origin", SAMPLE_ORIGINS)
def test_cors_regex_agrees_with_the_sdk_origin_validator(origin: str) -> None:
    """Match exactly what the SDK's transport-security middleware accepts

    A green preflight followed by a 403 on the real request is the failure this pins
    down, so the two policies are compared directly rather than to a fixed expectation.
    """
    settings, cors_origins = _resolve_transport_security([], [])
    middleware = TransportSecurityMiddleware(settings)

    cors_allows = re.compile(_cors_origin_regex(cors_origins)).fullmatch(origin) is not None

    assert cors_allows == middleware._validate_origin(origin)


def test_cors_regex_rejects_a_foreign_origin() -> None:
    """Reject an arbitrary web page outright, rather than answering it with ``*``"""
    _, cors_origins = _resolve_transport_security([], [])
    pattern = re.compile(_cors_origin_regex(cors_origins))

    assert pattern.fullmatch("https://evil.example") is None
    assert pattern.fullmatch("http://localhost:3000") is not None


# --- ASGI app selection -------------------------------------------------------------


def _captured_app(monkeypatch, argv: list[str]):
    """Run ``main`` far enough to capture the ASGI app it would serve.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        argv: Command-line arguments for the server entry point.

    Returns:
        The application handed to :func:`uvicorn.run`.
    """
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_run(app, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    server.main(argv)
    return captured


def _route_paths(app) -> set[str]:
    """List the paths served by a CORS-wrapped Starlette application.

    Args:
        app: The :class:`~starlette.middleware.cors.CORSMiddleware` instance.

    Returns:
        The path of every route on the wrapped application.
    """
    return {route.path for route in app.app.routes}


def test_sse_transport_serves_the_sse_routes(monkeypatch) -> None:
    """Serve ``/sse`` for ``--sse``

    ``streamable_http_app`` exists on every supported SDK version, so selecting the app
    by ``hasattr`` silently served Streamable HTTP and 404'd every existing SSE client.
    """
    captured = _captured_app(monkeypatch, ["--sse", "--host", "127.0.0.1", "--port", "8001"])

    assert "/sse" in _route_paths(captured["app"])
    assert captured["port"] == 8001


def test_streamable_http_transport_serves_the_mcp_route(monkeypatch) -> None:
    """Serve ``/mcp`` for ``--http``"""
    captured = _captured_app(monkeypatch, ["--http", "--host", "127.0.0.1", "--port", "8002"])

    assert "/mcp" in _route_paths(captured["app"])


def test_mount_path_is_forwarded_to_the_sse_app(monkeypatch) -> None:
    """Honour ``--mount-path``, which the HTTP branch had turned into a no-op

    ``mount_path`` does not rename the Starlette routes; it changes the message
    endpoint the SSE transport advertises to the client, which is what makes the server
    usable behind a proxy prefix. That advertised path is asserted here because the
    route set alone cannot tell the two branches apart.
    """
    import kindly_web_search_mcp_server.server as server

    # `mcp` is a module-level singleton, so the original value is restored on teardown.
    monkeypatch.setattr(server.mcp.settings, "mount_path", "/")
    _captured_app(monkeypatch, ["--sse", "--mount-path", "/kindly", "--port", "8003"])

    assert server.mcp.settings.mount_path == "/kindly"
    assert (
        server.mcp._normalize_path(
            server.mcp.settings.mount_path, server.mcp.settings.message_path
        )
        == "/kindly/messages/"
    )
