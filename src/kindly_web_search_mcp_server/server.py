from __future__ import annotations

import argparse
import logging
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .models import GetContentResponse, WebSearchResponse
from .content.resolver import resolve_page_content_markdown
from .search.serper import search_serper
from .utils.logging import configure_logging

configure_logging()
LOGGER = logging.getLogger(__name__)

mcp = FastMCP(
    "kindly-web-search",
    instructions=(
        "Web search via Serper with optional scraping/extraction of result pages "
        "into Markdown for LLM consumption."
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

    if not os.environ.get("SERPER_API_KEY"):
        # Do not hard-fail on startup: many clients set env vars in their MCP config
        # and expect the server to at least come up for tool discovery.
        LOGGER.warning("SERPER_API_KEY is not set; `web_search` calls will fail until it is provided.")

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
    return_full_pages: bool = True,
) -> dict:
    """Search the web and optionally return result pages as Markdown.

    This tool is intended to:\n
    1) query Serper (Google search API)\n
    2) return top results with `title`, `link`, `snippet`\n
    3) when requested, fetch each `link`, extract main content, and return it as
       `page_content` Markdown.\n

    Parameters
    - `query`: the search query string.
    - `num_results`: number of top search results to return (default: 3).
    - `return_full_pages`: when true, also include `page_content` Markdown for each
      result (default: true).

    Notes
    - Page content resolution is best-effort:
      - StackExchange links are fetched via the StackExchange API.
      - Other links fall back to a universal HTML loader (headless Nodriver) and are converted to Markdown.
    """

    results = await search_serper(query, num_results=num_results)

    if return_full_pages:
        enriched = []
        for r in results:
            page_md = await resolve_page_content_markdown(r.link)
            enriched.append(r.model_copy(update={"page_content": page_md}))
        results = enriched

    return WebSearchResponse(results=results).model_dump()


@mcp.tool()
async def get_content(url: str) -> dict:
    """Fetch a single URL and return its content as Markdown (best-effort).

    This tool reuses the same content resolution pipeline as `web_search(return_full_pages=true)`:
    - Specialized API loaders when available (e.g., StackExchange, GitHub Issues, Wikipedia, arXiv).
    - Universal HTML loader fallback for other web pages.

    Parameters
    - `url`: a URL to a web page (HTML) or an online document (best-effort).

    Returns
    - `{ "url": <url>, "page_content": <markdown> }`
    """
    page_md = await resolve_page_content_markdown(url)
    if page_md is None:
        # The current universal fallback intentionally skips obvious PDFs. Until we add a
        # generic PDF loader, return a deterministic Markdown note.
        page_md = f"_Could not retrieve content for this URL (possibly a PDF or unsupported type)._\\n\\nSource: {url}\\n"

    return GetContentResponse(url=url, page_content=page_md).model_dump()
