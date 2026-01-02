from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .extract import extract_content_as_markdown
from .sanitize import sanitize_markdown


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class UniversalHtmlLoaderConfig:
    """
    Configuration for universal HTML loading.

    Values are intentionally conservative to keep MCP tool calls bounded.
    """

    user_agent: str = DEFAULT_USER_AGENT
    wait_seconds: float = 2.0
    total_timeout_seconds: float = 20.0
    max_markdown_chars: int = 50_000


def _is_probably_pdf_url(url: str) -> bool:
    """Cheap heuristic: avoid HTML loader for obvious PDFs."""
    try:
        return urlparse(url).path.lower().endswith(".pdf")
    except Exception:
        return url.lower().endswith(".pdf")


def _maybe_add_src_to_pythonpath(env: dict[str, str]) -> dict[str, str]:
    """
    Ensure subprocesses can import this package when running from source.

    The example script modifies `sys.path` in-process (to include `./src`) so it can be executed
    without installing the package. Subprocesses do not inherit that mutation, so the universal
    loader sets `PYTHONPATH` to include `./src` when it exists.
    """
    try:
        # Anchor to this file's physical location instead of relying on cwd.
        # When running from source, this resolves to `<repo>/src`.
        src_dir = Path(__file__).resolve().parents[2]
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH", "")
            parts = [str(src_dir)]
            if existing:
                parts.append(existing)
            env["PYTHONPATH"] = os.pathsep.join(parts)
        return env
    except Exception:
        return env


async def fetch_html_via_nodriver(
    url: str,
    *,
    referer: str | None = None,
    config: UniversalHtmlLoaderConfig = UniversalHtmlLoaderConfig(),
) -> str:
    """
    Fetch a rendered HTML snapshot via headless Nodriver.

    Design constraints:
    - Keep the MCP stdio stream clean (no third-party debug prints).
    - Avoid Windows shutdown-time asyncio transport noise seen with in-process browser automation.

    Implementation detail:
    - A dedicated subprocess runs `kindly_web_search_mcp_server.scrape.nodriver_worker`.
    - The worker writes only HTML to stdout; all incidental output is discarded in the worker.
    """

    cmd = [
        sys.executable,
        "-m",
        "kindly_web_search_mcp_server.scrape.nodriver_worker",
        "--url",
        url,
        "--user-agent",
        config.user_agent,
        "--wait-seconds",
        str(config.wait_seconds),
    ]
    if referer:
        cmd.extend(["--referer", referer])

    env = _maybe_add_src_to_pythonpath(dict(os.environ))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.total_timeout_seconds
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            proc.kill()
        raise

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="ignore").strip()
        raise RuntimeError(
            f"nodriver worker failed (exit={proc.returncode}): {detail or 'unknown error'}"
        )

    return (stdout or b"").decode("utf-8", errors="ignore")


def html_to_markdown(
    html: str,
    *,
    source_url: str,
    config: UniversalHtmlLoaderConfig = UniversalHtmlLoaderConfig(),
) -> str:
    """
    Convert raw HTML to sanitized Markdown and cap output length.
    """
    markdown = extract_content_as_markdown(html)
    markdown = sanitize_markdown(markdown)
    if len(markdown) > config.max_markdown_chars:
        markdown = markdown[: config.max_markdown_chars].rstrip() + "\n\n…(truncated)\n"
    if markdown.strip() in ("", "Could not extract main content."):
        return f"_Could not extract main content._\n\nSource: {source_url}\n"
    return markdown


async def load_url_as_markdown(
    url: str,
    *,
    referer: str | None = None,
    config: UniversalHtmlLoaderConfig = UniversalHtmlLoaderConfig(),
) -> str | None:
    """
    Universal fallback: fetch HTML via headless Nodriver and return Markdown.

    Returns `None` for obvious non-HTML targets (e.g., PDFs).
    """
    if _is_probably_pdf_url(url):
        return None

    try:
        html = await fetch_html_via_nodriver(url, referer=referer, config=config)
    except Exception as exc:
        detail = str(exc).strip()
        if len(detail) > 400:
            detail = detail[:400].rstrip() + "…"
        suffix = f": {detail}" if detail else ""
        return f"_Failed to retrieve page content: {type(exc).__name__}{suffix}_\n\nSource: {url}\n"

    # If we somehow got a PDF/binary marker, refuse to parse it as HTML.
    if html.lstrip().startswith("%PDF-"):
        return None

    markdown = html_to_markdown(html, source_url=url, config=config)
    # Release the HTML buffer promptly (best-effort).
    html = ""
    return markdown
