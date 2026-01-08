from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
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
        value = float(raw) if raw else 6.0
    except ValueError:
        value = 6.0
    return max(0.5, min(value, 120.0))


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

    async with httpx.AsyncClient() as client:
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
) -> str:
    try:
        import nodriver as uc  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "nodriver is required for universal HTML loading. Install with: pip install nodriver"
        ) from exc

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

    base_browser_args = [
        "--window-size=1920,1080",
        *([] if sandbox_enabled else ["--no-sandbox"]),
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-logging",
        "--log-level=3",
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
                    last_start_error = None
                    break
                except Exception as exc:
                    last_start_error = exc
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

            if isinstance(content, (bytes, bytearray)):
                return bytes(content).decode("utf-8", errors="ignore")
            return str(content or "")
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

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _NullTextIO(original_stdout)
    sys.stderr = _NullTextIO(original_stderr)

    try:
        html = await _fetch_html(
            args.url,
            referer=args.referer,
            user_agent=args.user_agent,
            wait_seconds=args.wait_seconds,
            browser_executable_path=args.browser_executable_path,
        )
    except Exception as exc:
        # Keep stderr minimal (no traceback) to avoid bloating the parent error string.
        original_stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1

    # Emit only the HTML payload to stdout. Keep sys.stdout suppressed for the rest of
    # process lifetime so any shutdown/atexit prints from third-party libs are discarded.
    original_stdout.write(html)
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

    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
