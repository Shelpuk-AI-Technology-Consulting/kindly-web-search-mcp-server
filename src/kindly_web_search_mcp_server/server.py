from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .models import GetContentResponse, WebSearchResponse
from .content.resolver import resolve_page_content_markdown
from .search import search_web
from .utils.diagnostics import (
    Diagnostics,
    MAX_SAMPLE_CHARS,
    diagnostics_enabled,
    mask_env_values,
    new_request_id,
    sample_data,
)
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




def _get_int_env(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_float_env(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _resolve_tool_total_timeout_seconds() -> float:
    """
    Resolve the total per-tool time budget (seconds).

    Historically this was clamped to <=55s to stay below common 60s tool-call limits.
    In practice, Windows headless-browser cold starts can exceed that, so we allow a
    higher cap that can be tuned via environment variables.
    """
    value = _get_float_env("KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS", 120.0)
    max_value = _get_float_env("KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS", 600.0)
    safe_max = max(1.0, max_value)
    return max(1.0, min(value, safe_max))


def _resolve_web_search_max_concurrency(num_results: int) -> int:
    raw_env = (os.environ.get("KINDLY_WEB_SEARCH_MAX_CONCURRENCY") or "").strip()
    value: int | None = None
    if raw_env:
        try:
            parsed = int(raw_env)
        except ValueError:
            parsed = None
        if parsed and parsed > 0:
            value = parsed

    if value is None:
        value = 3
    value = max(1, min(value, 5))
    if num_results > 0:
        value = min(value, num_results)
    return value


def _timeout_markdown_note(url: str, *, scope: str | None = None) -> str:
    detail = f": {scope}" if scope else ""
    return f"_Failed to retrieve page content: TimeoutError{detail}_\n\nSource: {url}\n"

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
    - This tool is often called under a hard per-call deadline; page_content resolution is bounded by
      `KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS` (default 120, clamped 1..KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS) and concurrency is capped by
    `KINDLY_WEB_SEARCH_MAX_CONCURRENCY` (default 3 when unset; clamped 1..5).
    """

    started = time.monotonic()
    diag_enabled = diagnostics_enabled()
    parent_request_id = new_request_id() if diag_enabled else ""
    parent_diag = Diagnostics(parent_request_id, diag_enabled, stream=sys.stderr)
    if diag_enabled:
        env_snapshot = {
            "SERPER_API_KEY": os.environ.get("SERPER_API_KEY", ""),
            "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", ""),
            "SEARXNG_BASE_URL": os.environ.get("SEARXNG_BASE_URL", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS": os.environ.get("KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS", ""),
            "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": os.environ.get(
                "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS", ""
            ),
            "KINDLY_WEB_SEARCH_MAX_CONCURRENCY": os.environ.get("KINDLY_WEB_SEARCH_MAX_CONCURRENCY", ""),
        }
        parent_diag.emit(
            "web_search.start",
            "Starting web search",
            {
                "query": query,
                "num_results": num_results,
                "env": mask_env_values(env_snapshot),
            },
        )
    total_budget_seconds = _resolve_tool_total_timeout_seconds()

    results = await search_web(query, num_results=num_results, diagnostics=parent_diag)
    if not results:
        return WebSearchResponse(results=[]).model_dump(exclude_none=True)

    concurrency = _resolve_web_search_max_concurrency(len(results))
    semaphore = asyncio.Semaphore(concurrency)
    if diag_enabled:
        parent_diag.emit(
            "web_search.concurrency",
            "Resolved concurrency",
            {"concurrency": concurrency, "total_budget_seconds": total_budget_seconds},
        )

    async def enrich_one(r):
        result_diag = None
        if diag_enabled:
            result_request_id = new_request_id()
            result_diag = Diagnostics(
                result_request_id,
                True,
                stream=sys.stderr,
                context={"parent_request_id": parent_request_id, "url": r.link},
            )
            result_diag.emit("content.start", "Starting content fetch", {"url": r.link})
        async with semaphore:
            remaining = total_budget_seconds - (time.monotonic() - started)
            if remaining <= 0:
                page_md = _timeout_markdown_note(
                    r.link, scope="web_search time budget exceeded"
                )
                if result_diag:
                    result_diag.emit(
                        "content.timeout",
                        "Time budget exceeded before fetch",
                        {"remaining_seconds": remaining},
                    )
            else:
                try:
                    page_md = await asyncio.wait_for(
                        resolve_page_content_markdown(
                            r.link, diagnostics=result_diag
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    page_md = _timeout_markdown_note(r.link)
                    if result_diag:
                        result_diag.emit(
                            "content.timeout",
                            "Content fetch timed out",
                            {"remaining_seconds": remaining},
                        )
                except Exception as exc:
                    full_detail = str(exc).strip()
                    detail = full_detail
                    if len(detail) > 200:
                        detail = detail[:200].rstrip() + "…"
                    suffix = f": {type(exc).__name__}: {detail}" if detail else f": {type(exc).__name__}"
                    page_md = (
                        f"_Failed to retrieve page content{suffix}_\n\nSource: {r.link}\n"
                    )
                    if result_diag:
                        result_diag.emit(
                            "content.error",
                            "Content fetch failed",
                            {
                                "error": type(exc).__name__,
                                "detail": full_detail,
                                "detail_len": len(full_detail),
                            },
                        )

            if page_md is None:
                # The universal loader intentionally skips obvious PDFs; return a deterministic note.
                page_md = (
                    "_Could not retrieve content for this URL (possibly a PDF or unsupported type)._"
                    f"\n\nSource: {r.link}\n"
                )
                if result_diag:
                    result_diag.emit(
                        "content.skip",
                        "Content fetch skipped",
                        {"reason": "probable PDF or unsupported type"},
                    )

            if result_diag:
                result_diag.emit(
                    "content.result",
                    "Resolved content",
                    {
                        "content_len": len(page_md),
                        **sample_data(page_md, MAX_SAMPLE_CHARS),
                    },
                )
            return r.model_copy(
                update={
                    "page_content": page_md,
                    "diagnostics": result_diag.entries if result_diag else None,
                }
            )

    enriched = await asyncio.gather(*(enrich_one(r) for r in results))
    return WebSearchResponse(results=enriched).model_dump(exclude_none=True)


@mcp.tool()
async def get_content(url: str) -> dict:
    """Fetch a single URL and return best-effort, LLM-ready Markdown for that page.

    When to use:
    - You already have a URL (user provided it, or you found it via `web_search`).
    - You want to read/verify one specific source without doing a broader search.

    When not to use:
    - If you need to discover relevant URLs first or compare multiple sources → use `web_search(query)` instead.

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
    - This tool is often called under a hard per-call deadline; resolution is bounded by
      `KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS` (default 120, clamped 1..KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS).
    """

    timeout_seconds = _resolve_tool_total_timeout_seconds()
    diag_enabled = diagnostics_enabled()
    request_id = new_request_id() if diag_enabled else ""
    diag = Diagnostics(request_id, diag_enabled, stream=sys.stderr)
    if diag_enabled:
        env_snapshot = {
            "KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS": os.environ.get("KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS", ""),
            "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": os.environ.get(
                "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS", ""
            ),
            "KINDLY_BROWSER_EXECUTABLE_PATH": os.environ.get("KINDLY_BROWSER_EXECUTABLE_PATH", ""),
        }
        diag.emit(
            "get_content.start",
            "Starting content fetch",
            {"url": url, "env": mask_env_values(env_snapshot)},
        )

    try:
        page_md = await asyncio.wait_for(
            resolve_page_content_markdown(url, diagnostics=diag), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        page_md = _timeout_markdown_note(url, scope="tool time budget exceeded")
        if diag_enabled:
            diag.emit(
                "content.timeout",
                "Content fetch timed out",
                {"timeout_seconds": timeout_seconds},
            )
    except Exception as exc:
        full_detail = str(exc).strip()
        detail = full_detail
        if len(detail) > 200:
            detail = detail[:200].rstrip() + "…"
        suffix = f": {type(exc).__name__}: {detail}" if detail else f": {type(exc).__name__}"
        page_md = f"_Failed to retrieve page content{suffix}_\n\nSource: {url}\n"
        if diag_enabled:
            diag.emit(
                "content.error",
                "Content fetch failed",
                {"error": type(exc).__name__, "detail": full_detail, "detail_len": len(full_detail)},
            )

    if page_md is None:
        # The current universal fallback intentionally skips obvious PDFs. Until we add a
        # generic PDF loader, return a deterministic Markdown note.
        page_md = (
            "_Could not retrieve content for this URL (possibly a PDF or unsupported type)._"
            f"\n\nSource: {url}\n"
        )
        if diag_enabled:
            diag.emit("content.skip", "Content fetch skipped", {"reason": "probable PDF"})

    if diag_enabled:
        diag.emit(
            "content.result",
            "Resolved content",
            {"content_len": len(page_md), **sample_data(page_md, MAX_SAMPLE_CHARS)},
        )

    return GetContentResponse(
        url=url,
        page_content=page_md,
        diagnostics=diag.entries if diag_enabled else None,
    ).model_dump(exclude_none=True)
