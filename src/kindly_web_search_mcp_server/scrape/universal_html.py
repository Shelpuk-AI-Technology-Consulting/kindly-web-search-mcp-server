from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .chromium_pool import ChromiumSlot, get_chromium_pool, reuse_enabled
from .extract import extract_content_as_markdown
from .sanitize import sanitize_markdown
from .worker_runner import (
    _remove_worker_profile_directory,
    _run_pipe_probe,
    _run_worker_command,
)
from ..utils.diagnostics import (
    Diagnostics,
    MAX_SAMPLE_CHARS,
    mask_env_values,
    sample_data,
    truncate_text,
)


DEFAULT_USER_AGENT = (
    ""  # Empty triggers dynamic detection in nodriver_worker._resolve_user_agent
)


@dataclass(frozen=True)
class UniversalHtmlLoaderConfig:
    """
    Configuration for universal HTML loading.

    Values are intentionally conservative to keep MCP tool calls bounded.
    """

    user_agent: str = DEFAULT_USER_AGENT
    wait_seconds: float = 2.0
    total_timeout_seconds: float = 60.0
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


def _resolve_browser_executable_path() -> str | None:
    """
    Resolve a Chromium-based browser binary path for nodriver.

    This is required on some systems (notably fresh WSL/Linux installs) where
    no default Chrome/Chromium binary exists in standard locations.
    """
    for key in (
        "KINDLY_BROWSER_EXECUTABLE_PATH",
        "BROWSER_EXECUTABLE_PATH",
        "CHROME_BIN",
        "CHROME_PATH",
    ):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def _ensure_no_proxy_localhost_env(env: dict[str, str]) -> None:
    """
    Ensure Python subprocesses bypass proxies for loopback.

    The nodriver worker (and nodriver itself) may use urllib for `http://127.0.0.1:<port>/json/version`.
    If HTTP(S)_PROXY/ALL_PROXY are set without NO_PROXY/no_proxy, urllib can attempt to proxy loopback
    requests, leading to long hangs (commonly on Windows corporate machines).
    """
    raw = (env.get("KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return

    needed = ("localhost", "127.0.0.1", "::1")
    for key in ("NO_PROXY", "no_proxy"):
        existing = [x.strip() for x in (env.get(key) or "").split(",") if x.strip()]
        existing_lower = {x.lower() for x in existing}
        merged = list(existing)
        for host in needed:
            if host.lower() not in existing_lower:
                merged.append(host)
        if merged:
            env[key] = ",".join(merged)


def _split_worker_diagnostics(
    stderr_text: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    entries: list[dict[str, Any]] = []
    cleaned_lines: list[str] = []
    error_samples: list[str] = []
    for line in (stderr_text or "").splitlines():
        if not line.startswith("KINDLY_DIAG "):
            cleaned_lines.append(line)
            continue
        payload = line[len("KINDLY_DIAG ") :].strip()
        try:
            parsed = json.loads(payload)
        except Exception:
            if len(error_samples) < 3:
                sample, _, _ = truncate_text(payload, 200)
                error_samples.append(sample)
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
        else:
            if len(error_samples) < 3:
                sample, _, _ = truncate_text(payload, 200)
                error_samples.append(sample)
    cleaned_text = "\n".join(cleaned_lines).strip()
    return entries, cleaned_text, error_samples


def _build_worker_command(
    *,
    executable: str,
    url: str,
    referer: str | None,
    config: UniversalHtmlLoaderConfig,
    slot: ChromiumSlot | None,
    browser_executable_path: str | None,
    unpooled_user_data_dir: str | None = None,
) -> list[str]:
    """Build the command line that runs the nodriver worker child process.

    Pure: every input is a parameter, including the interpreter, so the result
    depends on nothing ambient. That is what makes the command shape assertable
    without spawning anything — see ``tests/test_worker_command_builder.py``.

    The returned list is also emitted verbatim into the ``worker.spawn``
    diagnostics entry, which is returned to the caller in the MCP response.
    Redacting the ``--url`` value there belongs to the diagnostics sanitization
    work, not here.

    Args:
        executable: Absolute path to the Python interpreter that runs the child.
            Taken as a parameter rather than read from :data:`sys.executable` so
            a test can tell "emits what it was given" from "emits the running
            interpreter".
        url: The page the worker should render.
        referer: Referer header for the worker to send, or ``None``. An empty
            string is treated as absent, matching the shipped behaviour.
        config: Loader configuration supplying the user agent and the render
            wait, the latter rendered as text because argv admits no other type.
        slot: A pooled Chromium slot to attach to, or ``None`` to let the worker
            start its own browser. The slot's own ``browser_executable_path`` is
            deliberately not read: the parent's resolved path wins, because that
            is the path the parent also propagates through the child's
            environment.
        browser_executable_path: Browser binary the worker should launch, or
            ``None`` to let it resolve one itself.
        unpooled_user_data_dir: Profile directory the caller has created for a
            worker that will launch its own browser, or ``None``. Read **only**
            when ``slot`` is ``None``, so the two sources of this one flag are
            mutually exclusive by construction rather than by a convention a
            caller has to remember: a pooled run gets the slot's own directory,
            an unpooled run gets the caller's, and neither can shadow the other.
            Like every other input it arrives as a parameter — this function
            creates nothing and its result still depends on nothing ambient.

    Returns:
        The full argv, interpreter first, ready for
        :func:`asyncio.create_subprocess_exec`.
    """
    # The invariant part: which interpreter runs which module against which URL.
    command = [
        executable,
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
        command.extend(["--referer", referer])

    # Attach to a pooled browser when one was acquired; the port can still be
    # unassigned, and zero is what the worker reads as "not yet known".
    if slot is not None:
        command.extend(
            [
                "--remote-host",
                slot.host,
                "--remote-port",
                str(slot.port or 0),
                "--reuse-browser",
            ]
        )
        if slot.user_data_dir is not None:
            command.extend(["--user-data-dir", slot.user_data_dir.name])
    # An unpooled worker used to create this directory inside itself, where a
    # killed worker could never remove it again. The caller owns it now, and
    # this branch is how the worker is told so.
    elif unpooled_user_data_dir:
        command.extend(["--user-data-dir", unpooled_user_data_dir])

    if browser_executable_path:
        command.extend(["--browser-executable-path", browser_executable_path])
    return command


async def fetch_html_via_nodriver(
    url: str,
    *,
    referer: str | None = None,
    config: UniversalHtmlLoaderConfig = UniversalHtmlLoaderConfig(),
    diagnostics: Diagnostics | None = None,
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

    pool = None
    slot = None
    # Bound before the `try`, because the `finally` that removes it has to see
    # it on every path -- including one that raises between the acquisition and
    # the assignment below.
    unpooled_user_data_dir: str | None = None
    # The acquisition sits inside the try whose `finally` returns the slot,
    # rather than before it. A slot acquired and then abandoned by a raise is
    # never queued again, and repeated occurrences starve the pool down to the
    # cold-browser fallback.
    try:
        if reuse_enabled():
            try:
                pool = await get_chromium_pool(diagnostics=diagnostics)
                slot = await pool.acquire(
                    user_agent=config.user_agent, diagnostics=diagnostics
                )
            except Exception as exc:
                if diagnostics:
                    diagnostics.emit(
                        "pool.error",
                        "Failed to acquire pooled Chromium",
                        {"error": type(exc).__name__},
                    )
                slot = None

        browser_executable_path = _resolve_browser_executable_path()
        # No slot means the worker would otherwise create this itself and then
        # be killed before it could remove it -- which is the disk half of the
        # orphaned-browser leak. `mkdtemp` rather than `TemporaryDirectory`:
        # ownership is explicit here, and a finalizer that fires on garbage
        # collection is exactly the mechanism that failed in the worker.
        if slot is None:
            unpooled_user_data_dir = tempfile.mkdtemp(prefix="kindly-nodriver-")
        cmd = _build_worker_command(
            executable=sys.executable,
            url=url,
            referer=referer,
            config=config,
            slot=slot,
            browser_executable_path=browser_executable_path,
            unpooled_user_data_dir=unpooled_user_data_dir,
        )

        env = _maybe_add_src_to_pythonpath(dict(os.environ))

        # Ensure nodriver can find the browser: if we have a resolved browser path,
        # propagate it via environment variables that nodriver recognizes.
        if browser_executable_path:
            env["KINDLY_BROWSER_EXECUTABLE_PATH"] = browser_executable_path
            env["BROWSER_EXECUTABLE_PATH"] = browser_executable_path
            env["CHROME_BIN"] = browser_executable_path

        if diagnostics and diagnostics.enabled:
            env["KINDLY_DIAGNOSTICS"] = "1"
            env["KINDLY_REQUEST_ID"] = diagnostics.request_id
        _ensure_no_proxy_localhost_env(env)

        if diagnostics and diagnostics.enabled:
            env["PYTHONUNBUFFERED"] = "1"
            diagnostics.emit(
                "worker.diagnostics_state",
                "Diagnostics state check",
                {
                    "enabled": diagnostics.enabled,
                    "type": diagnostics.__class__.__name__,
                    "probe_will_run": diagnostics.enabled,
                },
            )
            await _run_pipe_probe(
                executable=sys.executable,
                env=env,
                diagnostics=diagnostics,
            )

        def _emit_worker_spawn(active_cmd: list[str]) -> None:
            if diagnostics is None:
                return
            env_snapshot = {
                "KINDLY_BROWSER_EXECUTABLE_PATH": env.get(
                    "KINDLY_BROWSER_EXECUTABLE_PATH", ""
                ),
                "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS": env.get(
                    "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS", ""
                ),
                "KINDLY_NODRIVER_RETRY_ATTEMPTS": env.get(
                    "KINDLY_NODRIVER_RETRY_ATTEMPTS", ""
                ),
                "KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS": env.get(
                    "KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS", ""
                ),
                "KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS": env.get(
                    "KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS", ""
                ),
                "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER": env.get(
                    "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER", ""
                ),
                "KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST": env.get(
                    "KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST", ""
                ),
                "NO_PROXY": env.get("NO_PROXY", ""),
                "no_proxy": env.get("no_proxy", ""),
                "HTTP_PROXY": env.get("HTTP_PROXY", ""),
                "HTTPS_PROXY": env.get("HTTPS_PROXY", ""),
            }
            diagnostics.emit(
                "worker.spawn",
                "Launching nodriver worker",
                {
                    "url": url,
                    "referer": referer or "",
                    "user_agent": config.user_agent,
                    "wait_seconds": config.wait_seconds,
                    "cmd": active_cmd,
                    "env": mask_env_values(env_snapshot),
                },
            )

        _emit_worker_spawn(cmd)

        def _exception_message_chain(exc: Exception) -> str:
            parts: list[str] = []
            seen: set[int] = set()
            current: BaseException | None = exc
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                detail = str(current)
                if detail:
                    parts.append(detail)
                current = current.__cause__ or current.__context__
            return " | ".join(parts).lower()

        def _pool_error_requires_restart(exc: Exception) -> bool:
            message = _exception_message_chain(exc)
            patterns = (
                "nodriver worker failed",
                "protocol exception",
                "no browser is open",
                "failed to open new tab",
                "failed to create pooled target",
                "failed to connect to pooled browser",
                "devtools endpoint did not become ready",
                "connection refused",
            )
            return any(pattern in message for pattern in patterns)

        try:
            return await _run_worker_command(
                cmd,
                env=env,
                default_timeout_seconds=config.total_timeout_seconds,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            if slot is None or pool is None:
                raise
            if not _pool_error_requires_restart(exc):
                raise
            if diagnostics:
                diagnostics.emit(
                    "pool.slot_restart",
                    "Restarting pooled Chromium after worker failure",
                    {
                        "slot_id": slot.slot_id,
                        "error": type(exc).__name__,
                        "detail": _exception_message_chain(exc),
                    },
                )
            # Hand the stale slot back exactly once, with no path on which the
            # `finally` below returns it a second time. The order of these four
            # statements is load-bearing at every step. Terminate first, so a
            # terminate that raises leaves the slot still bound and the
            # `finally` recovers it. Release next, so a release that raises does
            # the same. Clear the local name only *after* the release and
            # *before* the re-acquire, so a re-acquire that raises finds nothing
            # left to release. `ChromiumPool.release` is an unconditional
            # `queue.put` with no membership check, so a slot queued twice hands
            # one browser, and one profile directory, to two concurrent callers.
            #
            # `slot = None` is not a dead write. The next statement rebinds it --
            # but only if that statement returns.
            stale = slot
            await stale.terminate()
            await pool.release(stale, diagnostics=diagnostics)
            slot = None
            slot = await pool.acquire(
                user_agent=config.user_agent, diagnostics=diagnostics
            )
            if slot is None:
                raise
            cmd = _build_worker_command(
                executable=sys.executable,
                url=url,
                referer=referer,
                config=config,
                slot=slot,
                # Always `None` here: this block is unreachable without a slot
                # (`if slot is None or pool is None: raise`, above), so the
                # replacement slot's own directory is what the worker gets.
                # Passed rather than omitted so the two call sites cannot drift.
                unpooled_user_data_dir=unpooled_user_data_dir,
                browser_executable_path=browser_executable_path,
            )
            _emit_worker_spawn(cmd)
            return await _run_worker_command(
                cmd,
                env=env,
                default_timeout_seconds=config.total_timeout_seconds,
                diagnostics=diagnostics,
            )
    finally:
        if slot is not None and pool is not None:
            await pool.release(slot, diagnostics=diagnostics)
        # A sibling of the release, never inside it. Nested under the pooled
        # branch this would run only for pooled runs -- the one case that must
        # delete nothing, because the directory belongs to a browser that is
        # still running and will serve the next caller.
        if unpooled_user_data_dir is not None:
            await _remove_worker_profile_directory(
                unpooled_user_data_dir, diagnostics=diagnostics
            )


def _apply_markdown_cap(
    markdown: str,
    config: UniversalHtmlLoaderConfig,
) -> str:
    """Apply the output length cap identically for every Markdown source.

    Shared by the browser path (``html_to_markdown``) and the markdown-suffix fast
    path so both emit the same ``…(truncated)`` marker once ``len(markdown)`` exceeds
    ``config.max_markdown_chars``. Byte-for-byte equivalent to the former inline cap.
    """
    if len(markdown) > config.max_markdown_chars:
        return markdown[: config.max_markdown_chars].rstrip() + "\n\n…(truncated)\n"
    return markdown


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
    markdown = _apply_markdown_cap(markdown, config)
    if markdown.strip() in ("", "Could not extract main content."):
        return f"_Could not extract main content._\n\nSource: {source_url}\n"
    return markdown


# --- Markdown-suffix fast path -------------------------------------------------
# Some docs platforms serve a clean markdown edition at `{path}.md`
# (Content-Type: text/markdown). For allowlisted hosts the loader probes that
# edition with one httpx GET before launching the headless browser, and falls
# back transparently on any miss. See docs/markdown-suffix-fast-path.md.
MD_SUFFIX_PROBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MIN_MD_SUFFIX_BYTES = 1024
MD_SUFFIX_PROBE_TIMEOUT_SECONDS = 5.0
_DEFAULT_MD_SUFFIX_HOSTS = "help.aliyun.com,www.alibabacloud.com/help"


def _load_md_suffix_hosts() -> list[tuple[str, str | None]]:
    """Parse ``KINDLY_MARKDOWN_SUFFIX_HOSTS`` into ``(host, path_prefix|None)``.

    Each entry is ``host`` (whole host) or ``host/path-prefix`` (scoped to a
    subtree). Empty/absent env disables the feature.
    """
    raw = os.environ.get(
        "KINDLY_MARKDOWN_SUFFIX_HOSTS", _DEFAULT_MD_SUFFIX_HOSTS
    ).strip()
    if not raw:
        return []
    out: list[tuple[str, str | None]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "/" in item:
            host, _, prefix = item.partition("/")
            host = host.strip().lower()
            prefix = ("/" + prefix.strip()).rstrip("/")
            if not host:
                continue
            out.append((host, prefix or None))
        else:
            out.append((item.lower(), None))
    return out


def _md_suffix_host_matches(url: str, hosts: list[tuple[str, str | None]]) -> bool:
    """True when ``url``'s host (and optional path prefix) is allowlisted."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    for entry_host, prefix in hosts:
        if entry_host != host:
            continue
        if prefix is None or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _build_md_suffix_url(url: str) -> str | None:
    """Rewrite ``url`` so its path ends with ``.md`` (before query/fragment).

    Returns ``None`` when the path is not a doc leaf (empty or ends with ``/``).
    Idempotent on ``.md``; maps ``.html`` -> ``.md``. Query and fragment are
    preserved so ``.md`` always lands before them.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = parsed.path or ""
    if not path or path.endswith("/"):
        return None
    if path.endswith(".md"):
        new_path = path
    elif path.endswith(".html"):
        new_path = path[: -len(".html")] + ".md"
    else:
        new_path = path + ".md"
    return parsed._replace(path=new_path).geturl()


async def _probe_markdown_suffix(
    url: str,
    *,
    config: UniversalHtmlLoaderConfig,
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """Probe ``{path}.md`` for allowlisted hosts; return capped Markdown or None.

    One httpx GET. On any miss -- non-allowlisted host, not a doc leaf, network
    error, non-200, content type other than ``text/markdown``, body under
    ``MIN_MD_SUFFIX_BYTES``, or empty after sanitize -- return ``None`` so the
    caller falls back to the browser path. Never raises into the caller.
    """
    hosts = _load_md_suffix_hosts()
    if not hosts or not _md_suffix_host_matches(url, hosts):
        return None
    md_url = _build_md_suffix_url(url)
    if md_url is None:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=MD_SUFFIX_PROBE_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            resp = await client.get(
                md_url,
                headers={
                    "User-Agent": MD_SUFFIX_PROBE_USER_AGENT,
                    "Accept": "text/markdown, text/html;q=0.5",
                },
            )
    except Exception as exc:
        if diagnostics:
            diagnostics.emit(
                "content.md_suffix_probe",
                "Markdown-suffix probe request failed",
                {
                    "result": "miss",
                    "reason": "request_error",
                    "md_url": md_url,
                    "error": type(exc).__name__,
                },
            )
        return None

    content_type = (
        (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    body_bytes = resp.content or b""
    if (
        resp.status_code != 200
        or content_type != "text/markdown"
        or len(body_bytes) < MIN_MD_SUFFIX_BYTES
    ):
        if diagnostics:
            diagnostics.emit(
                "content.md_suffix_probe",
                "Markdown-suffix probe missed",
                {
                    "result": "miss",
                    "reason": "validation_failed",
                    "md_url": md_url,
                    "status": resp.status_code,
                    "content_type": content_type,
                    "bytes": len(body_bytes),
                },
            )
        return None

    markdown = sanitize_markdown(body_bytes.decode("utf-8", errors="replace"))
    if not markdown.strip():
        if diagnostics:
            diagnostics.emit(
                "content.md_suffix_probe",
                "Markdown-suffix probe empty",
                {"result": "miss", "reason": "empty_body", "md_url": md_url},
            )
        return None

    markdown = _apply_markdown_cap(markdown, config)
    if diagnostics:
        diagnostics.emit(
            "content.md_suffix_probe",
            "Markdown-suffix probe hit",
            {
                "result": "hit",
                "md_url": md_url,
                "bytes": len(body_bytes),
                "content_type": content_type,
            },
        )
    return markdown


def _accept_probe_enabled() -> bool:
    """Whether the blanket Accept: text/markdown probe is enabled (default off)."""
    return (os.environ.get("KINDLY_MARKDOWN_ACCEPT_PROBE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _probe_markdown_accept_blanket(
    url: str,
    *,
    config: UniversalHtmlLoaderConfig,
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """Probe the URL as-is with ``Accept: text/markdown``; return Markdown or None.

    One httpx GET of the original URL (no path rewrite). On any miss -- network
    error, non-200, content type other than ``text/markdown``, body under
    ``MIN_MD_SUFFIX_BYTES``, or empty after sanitize -- return ``None`` so the
    caller falls back to the browser path (which re-fetches). Never raises into
    the caller. Only reached when ``KINDLY_MARKDOWN_ACCEPT_PROBE`` is enabled.
    """
    try:
        async with httpx.AsyncClient(
            timeout=MD_SUFFIX_PROBE_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": MD_SUFFIX_PROBE_USER_AGENT,
                    "Accept": "text/markdown, text/html;q=0.5",
                },
            )
    except Exception as exc:
        if diagnostics:
            diagnostics.emit(
                "content.md_accept_probe",
                "Markdown accept-probe request failed",
                {
                    "result": "miss",
                    "reason": "request_error",
                    "url": url,
                    "error": type(exc).__name__,
                },
            )
        return None

    content_type = (
        (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    body_bytes = resp.content or b""
    if (
        resp.status_code != 200
        or content_type != "text/markdown"
        or len(body_bytes) < MIN_MD_SUFFIX_BYTES
    ):
        if diagnostics:
            diagnostics.emit(
                "content.md_accept_probe",
                "Markdown accept-probe missed",
                {
                    "result": "miss",
                    "reason": "validation_failed",
                    "url": url,
                    "status": resp.status_code,
                    "content_type": content_type,
                    "bytes": len(body_bytes),
                },
            )
        return None

    markdown = sanitize_markdown(body_bytes.decode("utf-8", errors="replace"))
    if not markdown.strip():
        if diagnostics:
            diagnostics.emit(
                "content.md_accept_probe",
                "Markdown accept-probe empty",
                {"result": "miss", "reason": "empty_body", "url": url},
            )
        return None

    markdown = _apply_markdown_cap(markdown, config)
    if diagnostics:
        diagnostics.emit(
            "content.md_accept_probe",
            "Markdown accept-probe hit",
            {
                "result": "hit",
                "url": url,
                "bytes": len(body_bytes),
                "content_type": content_type,
            },
        )
    return markdown


async def load_url_as_markdown(
    url: str,
    *,
    referer: str | None = None,
    config: UniversalHtmlLoaderConfig = UniversalHtmlLoaderConfig(),
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """
    Universal fallback: fetch HTML via headless Nodriver and return Markdown.

    Returns `None` for obvious non-HTML targets (e.g., PDFs).
    """
    if _is_probably_pdf_url(url):
        if diagnostics:
            diagnostics.emit("content.skip", "Skipping probable PDF", {"url": url})
        return None

    # Markdown-suffix fast path: for allowlisted hosts, try `{path}.md` before
    # launching the headless browser. Returns None on any miss -> fall through.
    probed = await _probe_markdown_suffix(url, config=config, diagnostics=diagnostics)
    if probed is not None:
        return probed

    # Blanket Accept: text/markdown probe (opt-in via KINDLY_MARKDOWN_ACCEPT_PROBE).
    # For any URL the suffix path missed, ask the server for markdown; on text/html
    # or any other miss the browser re-fetches (the accepted double-fetch tax).
    if _accept_probe_enabled():
        probed = await _probe_markdown_accept_blanket(
            url, config=config, diagnostics=diagnostics
        )
        if probed is not None:
            return probed

    try:
        html = await fetch_html_via_nodriver(
            url, referer=referer, config=config, diagnostics=diagnostics
        )
    except Exception as exc:
        detail = str(exc).strip()
        if len(detail) > 400:
            detail = detail[:400].rstrip() + "…"
        suffix = f": {detail}" if detail else ""
        if diagnostics:
            diagnostics.emit(
                "content.error",
                "Universal HTML loader failed",
                {"error": type(exc).__name__, "detail": detail},
            )
        return f"_Failed to retrieve page content: {type(exc).__name__}{suffix}_\n\nSource: {url}\n"

    # If we somehow got a PDF/binary marker, refuse to parse it as HTML.
    if html.lstrip().startswith("%PDF-"):
        if diagnostics:
            diagnostics.emit("content.skip", "HTML looked like PDF", {"url": url})
        return None

    if diagnostics:
        diagnostics.emit(
            "content.html_sample",
            "Captured HTML sample",
            sample_data(html, MAX_SAMPLE_CHARS),
        )

    markdown = html_to_markdown(html, source_url=url, config=config)
    # Release the HTML buffer promptly (best-effort).
    html = ""
    return markdown
