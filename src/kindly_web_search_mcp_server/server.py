from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .models import GetContentResponse, WebSearchResponse
from .content.resolver import resolve_page_content_markdown
from .search import search_web
from .utils.logging import configure_logging

configure_logging()
LOGGER = logging.getLogger(__name__)

mcp = FastMCP(
    "kindly-web-search",
    instructions=(
        "Web search via Serper (default), Tavily, or a self-hosted SearXNG instance with best-effort "
        "scraping/extraction of result pages into Markdown for LLM consumption."
    ),
)

Transport = Literal["stdio", "sse", "streamable-http"]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-web-search",
        description="MCP server: Serper web search + robust content retrieval.",
    )

    transport_group = parser.add_mutually_exclusive_group()
    transport_group.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        help="Transport to use (default: stdio).",
    )
    transport_group.add_argument(
        "--stdio",
        dest="transport",
        action="store_const",
        const="stdio",
        help="Run using stdio transport (default).",
    )
    transport_group.add_argument(
        "--sse",
        dest="transport",
        action="store_const",
        const="sse",
        help="Run using SSE transport.",
    )
    transport_group.add_argument(
        "--http",
        "--streamable-http",
        dest="transport",
        action="store_const",
        const="streamable-http",
        help="Run using Streamable HTTP transport.",
    )

    parser.add_argument(
        "--host",
        default=None,
        help="Bind host for HTTP/SSE transports (overrides FASTMCP_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port for HTTP/SSE transports (overrides FASTMCP_PORT).",
    )
    parser.add_argument(
        "--mount-path",
        default=None,
        help="Mount path for SSE transport (if supported by the runtime).",
    )
    return parser


def _resolve_transport(raw: str | None) -> Transport:
    if raw in ("stdio", "sse", "streamable-http"):
        return raw
    return "stdio"


def _resolve_host_port(host: str | None, port: int | None) -> tuple[str, int]:
    resolved_host = host or os.environ.get("FASTMCP_HOST", "127.0.0.1")
    resolved_port_raw = str(port) if port is not None else os.environ.get("FASTMCP_PORT", "8000")
    try:
        resolved_port = int(resolved_port_raw)
    except ValueError:
        resolved_port = 8000
    return resolved_host, resolved_port


def main(argv: list[str] | None = None) -> None:
    """
    Entrypoint for running the MCP server.

    Notes:
    - Many MCP clients run servers via stdio by default.
    - HTTP/SSE transports are useful for containerized and gateway deployments.
    - FastMCP does not parse CLI args by itself; we do it here.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    transport = _resolve_transport(args.transport)

    if (
        transport == "stdio"
        and sys.stdin.isatty()
        and os.environ.get("MCP_ALLOW_TTY_STDIO", "").strip().lower() not in ("1", "true", "yes")
    ):
        print(
            "Error: `--stdio` transport is intended to be launched by an MCP client (stdin/stdout JSON-RPC).",
            file=sys.stderr,
        )
        print(
            "Tip: for manual testing, run with `--http` (Streamable HTTP) instead.",
            file=sys.stderr,
        )
        print(
            "Override: set MCP_ALLOW_TTY_STDIO=1 to force stdio even when stdin is a TTY.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not (
        os.environ.get("SERPER_API_KEY", "").strip()
        or os.environ.get("TAVILY_API_KEY", "").strip()
        or os.environ.get("SEARXNG_BASE_URL", "").strip()
    ):
        # Do not hard-fail on startup: many clients set env vars in their MCP config
        # and expect the server to at least come up for tool discovery.
        LOGGER.warning(
            "No search provider is configured (SERPER_API_KEY, TAVILY_API_KEY, or SEARXNG_BASE_URL); "
            "`web_search` calls will fail until one is provided."
        )

    if transport in ("sse", "streamable-http"):
        host, port = _resolve_host_port(args.host, args.port)
        # FastMCP settings are the source of truth for host/port in HTTP transports.
        # We mutate them at runtime to allow env/CLI overrides even if defaults were
        # passed during FastMCP initialization.
        for key, value in (("host", host), ("port", port)):
            if hasattr(mcp, "settings") and hasattr(mcp.settings, key):
                setattr(mcp.settings, key, value)

    try:
        mcp.run(transport=transport, mount_path=args.mount_path)
    except TypeError:
        # Backward-compat: older MCP SDKs may not accept `mount_path`.
        mcp.run(transport=transport)


@mcp.tool()
async def web_search(
    query: str,
    num_results: int = 3,
) -> dict:
    """Search the web and return top results with best-effort Markdown for each result URL.

    When to use:
    Especially useful for coding agents like Claude Code / Codex when you need up-to-date information.
    - Debug an error by searching the exact message/stack trace (often best in quotes).
    - Double-check API signatures, interfaces, and breaking changes in official docs.
    - Confirm current package versions, release notes, and migration guides.
    - Find GitHub issues / StackOverflow threads / authoritative references for a topic.

    When not to use:
    - If you already have a specific URL to read → use `get_content(url)` instead.

    Args:
    - query: Search query string. Prefer specific keywords and exact error text when applicable.
    - num_results: Number of results to return. Default is 3; recommended range is 1–5 to limit
      context size and keep results targeted.

    Prerequisites:
    - Requires at least one configured search provider in the server environment:
      `SERPER_API_KEY` (Serper), `TAVILY_API_KEY` (Tavily), or `SEARXNG_BASE_URL` (SearXNG).
      If none is set, this tool will fail.

    Returns:
    - `{"results": [{"title": str, "link": str, "snippet": str, "page_content": str}, ...]}`
    - `page_content` is always a string. If extraction fails (paywall/anti-bot/unsupported content),
      it becomes a deterministic Markdown note that includes the source URL.

    Notes:
    - Content extraction is best-effort and may be truncated to avoid context “bombs”.
    - Provider routing (strict order): Serper → Tavily → SearXNG. No cross-provider fallback.
    - If the search provider fails (missing key, quota/rate-limit, network issues), the tool will error.
    - For a deeper look at one result, call `get_content()` on the chosen `link`.
    """

    results = await search_web(query, num_results=num_results)

    enriched = []
    for r in results:
        page_md = await resolve_page_content_markdown(r.link)
        if page_md is None:
            # The universal loader intentionally skips obvious PDFs; return a deterministic note.
            page_md = (
                "_Could not retrieve content for this URL (possibly a PDF or unsupported type)._"
                f"\n\nSource: {r.link}\n"
            )
        enriched.append(r.model_copy(update={"page_content": page_md}))
    results = enriched

    return WebSearchResponse(results=results).model_dump()


@mcp.tool()
async def get_content(url: str) -> dict:
    """Fetch a single URL and return best-effort, LLM-ready Markdown for that page.

    When to use:
    - You already have a URL (user provided it, or you found it via `web_search`).
    - You want to read/verify one specific source without doing a broader search.

    When not to use:
    - If you need to discover relevant URLs first or compare multiple sources → use `web_search(query)`.

    Args:
    - url: A URL to a page/document to fetch.

    Returns:
    - `{"url": str, "page_content": str}`
    - `page_content` is always a string. If retrieval/extraction fails, it becomes a deterministic
      Markdown note that includes the source URL.

    Notes:
    - Uses the same content-resolution pipeline as `web_search`:
      - Specialized loaders for StackExchange, GitHub Issues, Wikipedia, and arXiv when applicable.
      - Otherwise a universal HTML loader (headless Nodriver).
    - Some content types (including many PDFs) may be unsupported.
    - Content extraction is best-effort and may be truncated.
    """
    page_md = await resolve_page_content_markdown(url)
    if page_md is None:
        # The current universal fallback intentionally skips obvious PDFs. Until we add a
        # generic PDF loader, return a deterministic Markdown note.
        page_md = f"_Could not retrieve content for this URL (possibly a PDF or unsupported type)._\\n\\nSource: {url}\\n"

    return GetContentResponse(url=url, page_content=page_md).model_dump()
