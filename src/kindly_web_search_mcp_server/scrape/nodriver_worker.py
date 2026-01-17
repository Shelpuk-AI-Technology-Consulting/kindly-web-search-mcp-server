from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
from typing import TextIO


class _NullTextIO(io.TextIOBase):
    """
    A text sink that discards writes but preserves file-descriptor APIs.

    Some third-party libraries write to sys.stdout/sys.stderr directly (instead of using logging).
    In MCP stdio mode, any accidental output can corrupt the protocol stream. The universal loader
    therefore runs browser automation in a subprocess and discards incidental output here.
    """

    def __init__(self, wrapped: TextIO) -> None:
        self._wrapped = wrapped

    def write(self, s: str) -> int:  # type: ignore[override]
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        return None

    def fileno(self) -> int:  # type: ignore[override]
        return self._wrapped.fileno()

    def isatty(self) -> bool:  # type: ignore[override]
        try:
            return self._wrapped.isatty()
        except Exception:
            return False

    @property
    def buffer(self):  # type: ignore[override]
        return getattr(self._wrapped, "buffer", None)


def _safe_write_text(stream: TextIO, text: str) -> None:
    """
    Best-effort write to a text stream without raising encoding errors.

    This worker intentionally suppresses sys.stdout/sys.stderr to keep MCP stdio clean.
    When a failure occurs, we still want *some* diagnostics to reach the parent process
    via stderr pipes; never let a UnicodeEncodeError erase the real error.
    """
    msg = (text or "").rstrip() + "\n"
    try:
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            buf.write(msg.encode("utf-8", errors="backslashreplace"))
            buf.flush()
            return
    except Exception:
        # Fall back to text write below.
        pass

    try:
        stream.write(msg)
        stream.flush()
    except Exception:
        # Last resort: try writing to fd=2 (stderr). Ignore failures.
        try:
            os.write(2, msg.encode("utf-8", errors="backslashreplace"))
        except Exception:
            return


def _safe_write_bytes(stream: TextIO, data: bytes) -> None:
    """
    Best-effort write raw bytes to a stream.

    On Windows, `sys.stdout` is often configured with a legacy codepage (e.g., cp1252).
    Writing HTML payloads as text can raise `UnicodeEncodeError`. For our subprocess
    protocol we want deterministic UTF-8 bytes on stdout regardless of console encoding.
    """
    payload = data or b""
    try:
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            buf.write(payload)
            buf.flush()
            return
    except Exception:
        pass

    try:
        os.write(1, payload)
    except Exception:
        return


_DIAG_ENABLED = False
_DIAG_REQUEST_ID = "unknown"
_DIAG_STREAM: TextIO | None = None
_DIAG_STARTED = 0.0
_DIAG_LINE_LIMIT = 8000  # Keep in sync with utils.diagnostics.MAX_LINE_CHARS


def _diagnostics_enabled() -> bool:
    raw = (os.environ.get("KINDLY_DIAGNOSTICS") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _emit_diag(stage: str, msg: str, data: dict[str, object] | None = None) -> None:
    if not _DIAG_ENABLED:
        return
    try:
        stream = _DIAG_STREAM or sys.stderr
        elapsed_ms = int((time.monotonic() - _DIAG_STARTED) * 1000)
        entry = {
            "request_id": _DIAG_REQUEST_ID,
            "stage": stage,
            "msg": msg,
            "elapsed_ms": elapsed_ms,
            "data": data or {},
        }
        payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
        if len(payload) > _DIAG_LINE_LIMIT:
            entry = {
                "request_id": _DIAG_REQUEST_ID,
                "stage": stage,
                "msg": msg,
                "elapsed_ms": elapsed_ms,
                "line_truncated": True,
                "data": {"note": "diagnostic payload truncated", "original_len": len(payload)},
            }
            payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
        _safe_write_text(stream, f"KINDLY_DIAG {payload}")
    except Exception:
        return


_CODING_COOKIE_RE = re.compile(r"coding[:=]\s*([-\w.]+)")


def _get_encoding_cookie(lines: list[bytes]) -> str | None:
    for idx in range(min(2, len(lines))):
        line = lines[idx]
        if idx == 0 and line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        try:
            text = line.decode("latin-1")
        except Exception:
            text = line.decode("latin-1", errors="ignore")
        match = _CODING_COOKIE_RE.search(text)
        if match:
            return match.group(1).lower()
    return None


def _has_encoding_cookie(lines: list[bytes]) -> bool:
    return _get_encoding_cookie(lines) is not None


def _line_ending_for(lines: list[bytes]) -> bytes:
    for line in lines[:2]:
        if line.endswith(b"\r\n"):
            return b"\r\n"
        if line.endswith(b"\n"):
            return b"\n"
    return b"\n"


def _inject_encoding_cookie(lines: list[bytes]) -> list[bytes]:
    line_ending = _line_ending_for(lines)
    # nodriver CDP sources contain non-UTF-8 bytes; latin-1 keeps raw bytes stable.
    cookie = b"# coding: latin-1" + line_ending
    if lines and lines[0].startswith(b"#!"):
        return [lines[0], cookie] + lines[1:]
    return [cookie] + lines


def _is_non_utf8_syntax_error(exc: SyntaxError) -> bool:
    msg = str(getattr(exc, "msg", "") or exc).lower()
    return "non-utf-8" in msg or "encoding problem" in msg


def _is_nodriver_network_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith("/nodriver/cdp/network.py")


def _resolve_nodriver_network_path(exc: SyntaxError) -> str | None:
    filename = getattr(exc, "filename", None)
    if filename:
        resolved = os.path.realpath(filename)
        if _is_nodriver_network_path(resolved):
            return resolved

    spec = importlib.util.find_spec("nodriver.cdp.network")
    if spec and spec.origin:
        resolved = os.path.realpath(spec.origin)
        if _is_nodriver_network_path(resolved):
            return resolved

    return None


def _clear_nodriver_modules() -> None:
    for key in list(sys.modules):
        if key == "nodriver" or key.startswith("nodriver."):
            sys.modules.pop(key, None)


def _patch_nodriver_network_encoding(exc: SyntaxError) -> bool:
    if not _is_non_utf8_syntax_error(exc):
        return False

    path = _resolve_nodriver_network_path(exc)
    if not path:
        return False

    try:
        with open(path, "rb") as handle:
            content = handle.read()
    except FileNotFoundError as file_exc:
        raise RuntimeError(f"nodriver network.py not found at {path}") from file_exc
    except PermissionError as file_exc:
        raise RuntimeError(f"nodriver network.py is not writable at {path}") from file_exc

    lines = content.splitlines(keepends=True)
    if _has_encoding_cookie(lines):
        return True

    updated_lines = _inject_encoding_cookie(lines)
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="._nodriver_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as tmp_handle:
            tmp_handle.writelines(updated_lines)
            tmp_handle.flush()
            os.fsync(tmp_handle.fileno())
        try:
            os.replace(tmp_path, path)
        except OSError as replace_exc:
            raise RuntimeError(f"Failed to replace nodriver network.py at {path}") from replace_exc
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

    return True


def _suppress_unraisable_exceptions() -> None:
    """
    Prevent shutdown-time "Exception ignored in: ..." noise from leaking to stderr.

    On Windows (Python 3.13), asyncio Proactor transports can raise unraisable exceptions
    during interpreter shutdown when third-party libs leave pipes/transports in odd states.
    This worker silences only known-noisy cases while preserving unexpected exceptions.
    """

    original = getattr(sys, "unraisablehook", None)
    if not callable(original):
        return

    def filtered(unraisable):  # type: ignore[no-untyped-def]
        exc = getattr(unraisable, "exc_value", None)
        msg = str(exc) if exc is not None else ""
        err_msg = str(getattr(unraisable, "err_msg", "") or "")

        if isinstance(exc, ValueError) and "I/O operation on closed pipe" in msg:
            return
        if "BaseSubprocessTransport.__del__" in err_msg or "ProactorBasePipeTransport.__del__" in err_msg:
            return

        return original(unraisable)

    sys.unraisablehook = filtered  # type: ignore[assignment]


def _resolve_browser_executable_path(explicit_path: str | None) -> str | None:
    if explicit_path and explicit_path.strip():
        return explicit_path.strip()

    for key in (
        "KINDLY_BROWSER_EXECUTABLE_PATH",
        "BROWSER_EXECUTABLE_PATH",
        "CHROME_BIN",
        "CHROME_PATH",
    ):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value

    for name in ("chromium", "google-chrome", "google-chrome-stable", "chrome", "chromium-browser"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    return None


def _resolve_sandbox_enabled() -> bool:
    """
    Determine whether Chromium sandbox should be enabled.

    - In containers, the server may run as root; Chromium generally cannot start with sandbox as root.
    - Default is sandbox disabled to improve headless reliability in WSL/Docker.
    """
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return False
    except Exception:
        pass

    raw_sandbox = (os.environ.get("KINDLY_NODRIVER_SANDBOX") or "").strip().lower()
    if raw_sandbox in ("0", "false", "no", "off"):
        return False
    if raw_sandbox in ("1", "true", "yes", "on"):
        return True
    return False


def _is_retryable_browser_connect_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    if "failed to connect to browser" in message:
        return True
    if "connection refused" in message:
        return True
    if "devtoolsactiveport" in message:
        return True
    if "devtools endpoint did not become ready" in message:
        return True
    return False


def _is_snap_browser(executable_path: str) -> bool:
    try:
        resolved = os.path.realpath(executable_path)
    except Exception:
        resolved = executable_path
    return resolved.startswith("/snap/") or "/snap/" in resolved


def _resolve_start_retry_attempts() -> int:
    raw = (os.environ.get("KINDLY_NODRIVER_RETRY_ATTEMPTS") or "").strip()
    try:
        value = int(raw) if raw else 3
    except ValueError:
        value = 3
    return max(1, min(value, 5))


def _resolve_retry_backoff_seconds() -> float:
    raw = (os.environ.get("KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 0.5
    except ValueError:
        value = 0.5
    return max(0.0, min(value, 10.0))


def _resolve_devtools_ready_timeout_seconds() -> float:
    """
    Maximum time to wait for Chromium's DevTools HTTP endpoint to become reachable.

    Notes:
    - The universal loader runs this worker in a subprocess with its own overall timeout.
      Keep defaults conservative and allow env overrides for slow cold starts (e.g., Snap).
    """
    raw = (os.environ.get("KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS") or "").strip()
    try:
        # Windows cold starts (first run + antivirus scans of fresh user-data-dir) can
        # easily exceed a 6s budget. Keep this bounded, but more forgiving by default.
        value = float(raw) if raw else 12.0
    except ValueError:
        value = 12.0
    return max(0.5, min(value, 120.0))


def _resolve_worker_timeout_seconds() -> float:
    raw = (os.environ.get("KINDLY_HTML_TOTAL_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 60.0
    except ValueError:
        value = 60.0
    value = max(1.0, min(value, 300.0))
    # Leave a grace window for parent-side cleanup on Windows.
    grace = min(10.0, max(5.0, value * 0.2))
    return max(1.0, value - grace)


def _split_no_proxy_value(raw: str) -> list[str]:
    out: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if value:
            out.append(value)
    return out


def _ensure_no_proxy_localhost() -> None:
    """
    Ensure Python stdlib proxy handling does not hijack localhost traffic.

    Why:
    - Nodriver's internal DevTools readiness checks use urllib for `/json/version`.
    - On Windows, corporate environments often set HTTP(S)_PROXY/ALL_PROXY globally.
    - If NO_PROXY/no_proxy doesn't include loopback hosts, urllib may attempt to proxy
      `http://127.0.0.1:<port>/json/version`, causing long hangs and timeouts.

    This is safe for our workload because:
    - External navigation uses Chromium's network stack, not urllib.
    - We only need to guarantee loopback bypass for the local DevTools endpoint.
    """
    raw = (os.environ.get("KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return

    needed = ("localhost", "127.0.0.1", "::1")
    for key in ("NO_PROXY", "no_proxy"):
        existing = _split_no_proxy_value((os.environ.get(key) or "").strip())
        existing_lower = {x.lower() for x in existing}
        merged = list(existing)
        for host in needed:
            if host.lower() not in existing_lower:
                merged.append(host)
        os.environ[key] = ",".join(merged)


def _pick_free_port(host: str = "127.0.0.1") -> int:
    # Best-effort selection: inherently racy, so startup must tolerate collisions.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _resolve_snap_backoff_multiplier() -> float:
    raw = (os.environ.get("KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER") or "").strip()
    try:
        value = float(raw) if raw else 3.0
    except ValueError:
        value = 3.0
    return max(1.0, min(value, 20.0))


def _build_chromium_launch_args(
    *,
    base_browser_args: list[str],
    user_data_dir: str,
    user_agent: str,
    host: str,
    port: int,
    sandbox_enabled: bool,
) -> list[str]:
    args: list[str] = [
        # Ensure we only bind DevTools to loopback.
        f"--remote-debugging-host={host}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # Keep consistent with our previous nodriver.start() behavior.
        "--headless=new",
        "--window-size=1920,1080",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-logging",
        "--log-level=3",
        f"--user-agent={user_agent}",
        *([] if sandbox_enabled else ["--no-sandbox"]),
    ]

    # Append the base args last to preserve existing behavior (and allow overrides),
    # while avoiding duplicates that can confuse Chromium.
    for item in base_browser_args:
        if item not in args:
            args.append(item)
    return args


async def _launch_chromium(
    executable_path: str,
    args: list[str],
) -> asyncio.subprocess.Process:
    # Discard Chromium stdout/stderr to avoid deadlocks on filled pipes.
    return await asyncio.create_subprocess_exec(
        executable_path,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )


async def _terminate_process(proc: asyncio.subprocess.Process, *, grace_seconds: float = 1.5) -> None:
    try:
        if proc.returncode is not None:
            return

        terminated = False
        if os.name == "posix" and proc.pid is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                terminated = True
            except Exception:
                terminated = False
        if not terminated:
            with contextlib.suppress(Exception):
                proc.terminate()

        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            return
        except Exception:
            pass

        if os.name == "posix" and proc.pid is not None:
            with contextlib.suppress(Exception):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
    except Exception:
        return


async def _wait_for_devtools_ready(
    *,
    host: str,
    port: int,
    proc: asyncio.subprocess.Process,
    timeout_seconds: float,
) -> None:
    """
    Wait until the DevTools HTTP endpoint responds.

    Chrome exposes `webSocketDebuggerUrl` via GET `/json/version`. This is a stronger readiness signal
    than a raw TCP connect because it requires the browser to be responsive, not just listening.
    """
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("httpx is required for DevTools readiness probing") from exc

    deadline = time.monotonic() + max(0.1, timeout_seconds)
    url = f"http://{host}:{port}/json/version"

    # Never allow proxy/VPN env vars to hijack localhost traffic. On Windows in particular,
    # corporate environments often set HTTP(S)_PROXY/ALL_PROXY globally, and missing NO_PROXY
    # for 127.0.0.1 can cause the readiness probe to hang or fail.
    async with httpx.AsyncClient(trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise RuntimeError(f"Chromium exited early (code={proc.returncode})")
            try:
                resp = await client.get(url, timeout=0.75)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)

    raise RuntimeError("DevTools endpoint did not become ready in time")


async def _fetch_html(
    url: str,
    *,
    referer: str | None,
    user_agent: str,
    wait_seconds: float,
    browser_executable_path: str | None,
    overall_timeout_seconds: float,
) -> str:
    try:
        import nodriver as uc  # type: ignore
    except SyntaxError as exc:  # pragma: no cover
        should_retry = _patch_nodriver_network_encoding(exc)
        if should_retry:
            _clear_nodriver_modules()
            importlib.invalidate_caches()
            try:
                import nodriver as uc  # type: ignore
            except Exception as retry_exc:
                raise RuntimeError(
                    "nodriver import failed after encoding check. "
                    f"Original error: {exc}. Retry error: {retry_exc}"
                ) from retry_exc
        else:
            raise RuntimeError(
                "nodriver is required for universal HTML loading. Install with: pip install nodriver"
            ) from exc
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "nodriver is required for universal HTML loading. Install with: pip install nodriver"
        ) from exc

    started = time.monotonic()
    browser = None
    page = None
    ref_page = None
    chrome_proc: asyncio.subprocess.Process | None = None
    sandbox_enabled = _resolve_sandbox_enabled()
    resolved_browser_executable_path = _resolve_browser_executable_path(browser_executable_path)
    if resolved_browser_executable_path is None:
        raise RuntimeError(
            "No Chromium-based browser executable found. "
            "Install Chromium/Chrome or set KINDLY_BROWSER_EXECUTABLE_PATH to the browser binary path."
        )
    is_snap = _is_snap_browser(resolved_browser_executable_path)
    attempts = _resolve_start_retry_attempts()
    base_backoff_seconds = _resolve_retry_backoff_seconds()
    snap_multiplier = _resolve_snap_backoff_multiplier() if is_snap else 1.0
    devtools_ready_timeout_seconds = _resolve_devtools_ready_timeout_seconds() * snap_multiplier
    _emit_diag(
        "worker.config",
        "Resolved browser configuration",
        {
            "browser_executable_path": resolved_browser_executable_path,
            "sandbox_enabled": sandbox_enabled,
            "is_snap": is_snap,
            "attempts": attempts,
            "backoff_seconds": base_backoff_seconds,
            "devtools_ready_timeout_seconds": devtools_ready_timeout_seconds,
            "overall_timeout_seconds": overall_timeout_seconds,
        },
    )

    base_browser_args = [
        "--window-size=1920,1080",
        *([] if sandbox_enabled else ["--no-sandbox"]),
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-logging",
        "--log-level=3",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        f"--user-agent={user_agent}",
    ]

    # Chromium may still be flushing profile writes briefly after `browser.stop()`.
    # Never fail the request because a temp profile directory couldn't be deleted.
    with tempfile.TemporaryDirectory(prefix="kindly-nodriver-", ignore_cleanup_errors=True) as user_data_dir:
        try:
            last_start_error: BaseException | None = None
            for attempt in range(attempts):
                try:
                    host = "127.0.0.1"
                    port = _pick_free_port(host)
                    _emit_diag(
                        "worker.launch_attempt",
                        "Launching Chromium",
                        {"attempt": attempt + 1, "host": host, "port": port},
                    )
                    chromium_args = _build_chromium_launch_args(
                        base_browser_args=base_browser_args,
                        user_data_dir=user_data_dir,
                        user_agent=user_agent,
                        host=host,
                        port=port,
                        sandbox_enabled=sandbox_enabled,
                    )
                    chrome_proc = await _launch_chromium(resolved_browser_executable_path, chromium_args)
                    await _wait_for_devtools_ready(
                        host=host,
                        port=port,
                        proc=chrome_proc,
                        timeout_seconds=devtools_ready_timeout_seconds,
                    )
                    _emit_diag(
                        "worker.devtools_ready",
                        "DevTools endpoint ready",
                        {"host": host, "port": port},
                    )

                    # Connect Nodriver to the already-running browser instance (do not spawn another).
                    browser = await uc.start(
                        headless=True,
                        user_data_dir=user_data_dir,
                        browser_executable_path=resolved_browser_executable_path,
                        sandbox=sandbox_enabled,
                        browser_args=base_browser_args,
                        host=host,
                        port=port,
                    )
                    _emit_diag(
                        "worker.browser_started",
                        "Nodriver connected to browser",
                        {"host": host, "port": port},
                    )
                    last_start_error = None
                    break
                except Exception as exc:
                    last_start_error = exc
                    _emit_diag(
                        "worker.launch_error",
                        "Browser launch attempt failed",
                        {"attempt": attempt + 1, "error": type(exc).__name__},
                    )
                    if chrome_proc is not None:
                        await _terminate_process(chrome_proc)
                        chrome_proc = None
                    if attempt >= attempts - 1 or not _is_retryable_browser_connect_error(exc):
                        raise
                    backoff = base_backoff_seconds * (2**attempt) * snap_multiplier
                    await asyncio.sleep(backoff)

            if browser is None:
                raise RuntimeError(
                    f"nodriver failed to start browser after {attempts} attempt(s)"
                ) from last_start_error

            async def _navigate_and_extract() -> str:
                nonlocal page, ref_page
                if referer:
                    ref_page = await browser.get(referer)
                    await asyncio.sleep(0.25)

                page = await browser.get(url)
                await asyncio.sleep(wait_seconds)

                getter = getattr(page, "get_content", None)
                if callable(getter):
                    content = getter()
                    if asyncio.iscoroutine(content):
                        content = await content
                else:
                    getter = getattr(page, "content", None)
                    content = getter()
                    if asyncio.iscoroutine(content):
                        content = await content
                return str(content or "")

            remaining = overall_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Navigation timed out before start")
            try:
                content = await asyncio.wait_for(_navigate_and_extract(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Navigation timed out after {overall_timeout_seconds:.1f}s"
                ) from exc
            _emit_diag(
                "worker.navigate_complete",
                "Navigation complete",
                {"content_len": len(content) if isinstance(content, str) else 0},
            )

            if isinstance(content, (bytes, bytearray)):
                return bytes(content).decode("utf-8", errors="ignore")
            return content
        except Exception as exc:
            msg = str(exc).lower()
            if "failed to connect to browser" in msg or "devtools endpoint did not become ready" in msg:
                is_root = False
                try:
                    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
                except Exception:
                    is_root = False
                raise RuntimeError(
                    f"Failed to connect to browser after {attempts} attempt(s). "
                    f"(root={is_root}, sandbox={sandbox_enabled}, browser_executable_path={resolved_browser_executable_path!r}) "
                    "If running as root (e.g., in Docker), ensure sandbox is disabled (KINDLY_NODRIVER_SANDBOX=0). "
                    "If the browser cannot be found/started, set KINDLY_BROWSER_EXECUTABLE_PATH."
                ) from exc
            _emit_diag(
                "worker.error",
                "Worker failed during navigation",
                {"error": type(exc).__name__},
            )
            raise
        finally:
            # Best-effort cleanup. Errors here should not mask the page retrieval.
            try:
                for maybe_page in (page, ref_page):
                    if maybe_page is None:
                        continue
                    closer = getattr(maybe_page, "close", None)
                    if callable(closer):
                        maybe = closer()
                        if asyncio.iscoroutine(maybe):
                            await maybe

                if browser is not None:
                    stopper = getattr(browser, "stop", None)
                    if callable(stopper):
                        maybe = stopper()
                        if asyncio.iscoroutine(maybe):
                            await maybe

                if chrome_proc is not None:
                    await _terminate_process(chrome_proc)
                    chrome_proc = None
                # Give Chromium a short moment to flush profile writes before temp cleanup.
                await asyncio.sleep(0.1)
            except Exception:
                pass


async def _main_async(args: argparse.Namespace) -> int:
    _suppress_unraisable_exceptions()
    _ensure_no_proxy_localhost()

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    global _DIAG_ENABLED, _DIAG_REQUEST_ID, _DIAG_STREAM, _DIAG_STARTED
    _DIAG_ENABLED = _diagnostics_enabled()
    _DIAG_REQUEST_ID = (os.environ.get("KINDLY_REQUEST_ID") or "unknown").strip() or "unknown"
    _DIAG_STREAM = original_stderr
    _DIAG_STARTED = time.monotonic()
    if _DIAG_ENABLED:
        _emit_diag(
            "worker.start",
            "Worker starting",
            {
                "url": args.url,
                "referer": args.referer or "",
                "user_agent": args.user_agent,
                "wait_seconds": args.wait_seconds,
                "browser_executable_path": args.browser_executable_path or "",
            },
        )
    sys.stdout = _NullTextIO(original_stdout)
    sys.stderr = _NullTextIO(original_stderr)

    worker_timeout_seconds = _resolve_worker_timeout_seconds()
    try:
        html = await _fetch_html(
            args.url,
            referer=args.referer,
            user_agent=args.user_agent,
            wait_seconds=args.wait_seconds,
            browser_executable_path=args.browser_executable_path,
            overall_timeout_seconds=worker_timeout_seconds,
        )
    except Exception as exc:
        # Keep stderr minimal (no traceback) to avoid bloating the parent error string.
        _emit_diag(
            "worker.error",
            "Worker failed",
            {"error": type(exc).__name__, "detail": str(exc)},
        )
        _safe_write_text(original_stderr, f"{type(exc).__name__}: {exc}")
        return 1

    # Emit only the HTML payload to stdout. Keep sys.stdout suppressed for the rest of
    # process lifetime so any shutdown/atexit prints from third-party libs are discarded.
    _safe_write_bytes(original_stdout, (html or "").encode("utf-8", errors="strict"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch rendered HTML via headless nodriver.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--referer", required=False, default=None)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--wait-seconds", type=float, default=2.0)
    parser.add_argument("--browser-executable-path", required=False, default=None)
    args = parser.parse_args()

    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    original_stderr = sys.stderr
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # pragma: no cover
        # `_main_async` suppresses sys.stderr; if an unexpected exception escapes, ensure we
        # still emit *some* error detail to the parent process.
        _safe_write_text(original_stderr, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
