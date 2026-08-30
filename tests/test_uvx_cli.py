from __future__ import annotations

import os


def test_start_mcp_server_injects_stdio_and_context(monkeypatch) -> None:
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv
        captured["context"] = os.environ.get("KINDLY_MCP_CONTEXT")

    monkeypatch.setattr(server, "main", fake_server_main)

    assert os.environ.get("KINDLY_MCP_CONTEXT") is None
    cli.main(["start-mcp-server", "--context", "codex"])

    assert captured["argv"] == ["--stdio"]
    assert captured["context"] == "codex"
    assert os.environ.get("KINDLY_MCP_CONTEXT") is None


def test_start_mcp_server_forwards_server_args(monkeypatch) -> None:
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv
        captured["context"] = os.environ.get("KINDLY_MCP_CONTEXT")

    monkeypatch.setattr(server, "main", fake_server_main)

    cli.main(
        [
            "start-mcp-server",
            "--context",
            "codex",
            "--http",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]
    )

    assert captured["argv"] == ["--http", "--host", "127.0.0.1", "--port", "8000"]
    assert captured["context"] == "codex"


def test_start_mcp_server_drops_double_dash_separator(monkeypatch) -> None:
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(server, "main", fake_server_main)

    cli.main(["start-mcp-server", "--context", "codex", "--", "--http"])
    assert captured["argv"] == ["--http"]


def test_start_mcp_server_restores_existing_context(monkeypatch) -> None:
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv
        captured["context"] = os.environ.get("KINDLY_MCP_CONTEXT")

    monkeypatch.setattr(server, "main", fake_server_main)
    monkeypatch.setenv("KINDLY_MCP_CONTEXT", "existing")

    cli.main(["start-mcp-server", "--context", "codex"])

    assert captured["argv"] == ["--stdio"]
    assert captured["context"] == "codex"
    assert os.environ.get("KINDLY_MCP_CONTEXT") == "existing"


def test_start_mcp_server_honours_transport_env_var(monkeypatch) -> None:
    """Leave the transport to the server when FASTMCP_TRANSPORT selects one

    Injecting `--stdio` unconditionally made this the one entry point where
    FASTMCP_TRANSPORT was ignored, so a container configured for HTTP came up in
    stdio and exited immediately.
    """
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(server, "main", fake_server_main)
    monkeypatch.setenv("FASTMCP_TRANSPORT", "http")

    cli.main(["start-mcp-server", "--context", "codex"])

    assert captured["argv"] == []


def test_start_mcp_server_ignores_blank_transport_env_var(monkeypatch) -> None:
    """Still default to stdio when FASTMCP_TRANSPORT is set but blank"""
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(server, "main", fake_server_main)
    monkeypatch.setenv("FASTMCP_TRANSPORT", "   ")

    cli.main(["start-mcp-server"])

    assert captured["argv"] == ["--stdio"]


def test_start_mcp_server_flag_still_wins_over_transport_env_var(monkeypatch) -> None:
    """Forward an explicit flag untouched even when the env var disagrees"""
    from kindly_web_search_mcp_server import cli
    import kindly_web_search_mcp_server.server as server

    captured: dict[str, object] = {}

    def fake_server_main(argv: list[str] | None = None) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(server, "main", fake_server_main)
    monkeypatch.setenv("FASTMCP_TRANSPORT", "http")

    cli.main(["start-mcp-server", "--stdio"])

    assert captured["argv"] == ["--stdio"]
