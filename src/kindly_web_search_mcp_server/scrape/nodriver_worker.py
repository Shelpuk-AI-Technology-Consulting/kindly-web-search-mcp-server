from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
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
    sandbox_enabled = True
    raw_sandbox = (os.environ.get("KINDLY_NODRIVER_SANDBOX") or "").strip().lower()
    if raw_sandbox in ("0", "false", "no", "off"):
        sandbox_enabled = False
    elif raw_sandbox in ("1", "true", "yes", "on"):
        sandbox_enabled = True
    else:
        # Default: disable sandbox for headless automation to work reliably in WSL/Docker.
        sandbox_enabled = False

    try:
        browser = await uc.start(
            headless=True,
            browser_executable_path=browser_executable_path,
            sandbox=sandbox_enabled,
            browser_args=[
                "--window-size=1920,1080",
                *([] if sandbox_enabled else ["--no-sandbox"]),
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-logging",
                "--log-level=3",
                f"--user-agent={user_agent}",
            ],
        )

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
