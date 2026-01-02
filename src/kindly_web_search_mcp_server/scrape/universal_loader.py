"""Universal URL loader for HTML and PDF content.

This module provides a **single entrypoint** (`load_url`) that tries to turn an arbitrary URL
into one or more **LangChain `Document`** objects. It is intentionally browser-automation-heavy:

- **HTML**: render in a real browser (Nodriver/Chromium first, Selenium as fallback), then
  extract human-readable text from the resulting HTML.
- **PDF**: download bytes via browser automation (Nodriver first, Selenium as fallback), then
  parse the PDF into Markdown-like text via PyMuPDF4LLM.
- **Domain-specific fast paths**: ResearchGate → DOI → Unpaywall, and bioRxiv/medRxiv → S3 MECA/PDF.

Why browsers (vs `requests`)?
- Many sites block plain HTTP clients (Cloudflare/WAF), require JS rendering, or require cookies
  that are easiest to obtain by behaving like a real browser.

Important integration notes for this repository:
- This file was copied from another project and is currently **not wired** into the MCP server
  scraping pipeline. Treat it as an experimental/optional loader.
- The module imports optional heavy dependencies (`nodriver`, `selenium`, `langchain_core`,
  `bs4`, `pymupdf4llm`, etc.). If you import this module, ensure those dependencies are installed.

Key environment variables (all optional; safe defaults exist):
- `IPK_USER_AGENT`: user-agent string for both Nodriver and Selenium.
- `SELENIUM_BROWSER`: `chrome` (default) or `firefox`.
- `SELENIUM_HEADLESS`: set to `0/false/no` to disable headless mode.
- `SELENIUM_DEBUGGER_ADDRESS`: connect Selenium to an existing Chrome (do not manage lifecycle).
- `SELENIUM_FIREFOX_PROFILE_PATH`, `SELENIUM_FIREFOX_BINARY`: use an existing Firefox profile/binary.
- `SELENIUM_DOWNLOAD_TIMEOUT_SECONDS`, `SELENIUM_PAGELOAD_TIMEOUT_SECONDS`: Selenium timeouts.
- `NODRIVER_WAIT_SECONDS`, `NODRIVER_TOTAL_TIMEOUT_SECONDS`, `NODRIVER_WAF_WAIT_SECONDS`: Nodriver timing.
- `NODRIVER_PDF_DEBUG=1`: enable verbose PDF debug logs.
- `TOR_SOCKS_PORT`: send browser traffic through Tor via a SOCKS proxy (best-effort).
- `UNPAYWALL_EMAIL`: required for Unpaywall DOI lookup.
- `BIORXIV_S3_ACCESS_KEY`, `BIORXIV_S3_SECRET_KEY`: enable S3 fast path for bio/medrxiv.

The output is always a `list[Document]` (empty list on timeout). The loader is defensive and
tries multiple strategies before failing, while avoiding infinite retries.
"""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import concurrent.futures
import atexit
import base64
import logging
import asyncio
import threading
from typing import List
from urllib.parse import urlparse, urlsplit

from bs4 import BeautifulSoup
from langchain_core.documents import Document

LOGGER = logging.getLogger(__name__)

# Reuse a single asyncio loop per thread for Nodriver operations.
# Nodriver's `nodriver.loop()` creates a new event loop every call; using it in a
# long-running process leaks file descriptors until EMFILE.
_NODRIVER_LOOP_LOCAL = threading.local()
_NODRIVER_LOOPS: list[asyncio.AbstractEventLoop] = []


def _close_nodriver_loops_best_effort() -> None:
    loops = list(_NODRIVER_LOOPS)
    _NODRIVER_LOOPS.clear()
    for loop in loops:
        try:
            if loop.is_closed():
                continue
            loop.close()
        except Exception as exc:
            LOGGER.debug("Failed to close Nodriver asyncio loop: %s", exc)


atexit.register(_close_nodriver_loops_best_effort)

# ------------------------------ Constants & Configuration ------------------------------
UA = os.environ.get(
    "IPK_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
)
TOR_SOCKS_PORT: int | None = None
CANVAS_SPOOF_SCRIPT = """
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
Object.defineProperty(HTMLCanvasElement.prototype, "toDataURL", {
    value: function() {
        const context = this.getContext("2d");
        if (!context) {
            return originalToDataURL.apply(this, arguments);
        }
        const width = this.width;
        const height = this.height;
        if (!width || !height) {
            return originalToDataURL.apply(this, arguments);
        }
        const imageData = context.getImageData(0, 0, width, height);
        for (let i = 0; i < 5; i++) {
            imageData.data[i] = imageData.data[i] + (Math.floor(Math.random() * 4) - 2);
        }
        context.putImageData(imageData, 0, 0);
        return originalToDataURL.apply(this, arguments);
    }
});
"""

MAX_PDF = 100 * 1024 * 1024  # 100 MB
BIORXIV_S3_BUCKET = "biorxiv-src-monthly"
MEDRXIV_S3_BUCKET = "medrxiv-src-monthly"
S3_MECA_CACHE: dict[str, str] = {}
S3_MONTH_KEYS_CACHE: dict[str, list[str]] = {}

# Selenium-only runtime: no shared requests session.

CF_MARKERS = (
    "just a moment",
    "ray id",
    "verify you are human",
    "cf-please-wait",
    # Additional common WAF/anti-bot phrasing
    "confirm you are human",
    "let's confirm you are human",
    "complete the security check",
    "checking your browser",
)
CF_SELECTORS = (
    "#cf-spinner-please-wait",
    "#cf-please-wait",
    "#challenge-spinner",
    "#challenge-running",
    ".cf-turnstile-wrapper",
    'text="Just a moment"',
    'text="Verifying you are human"',
)

_WINDOWS_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None

RG_BLOCK_MARKERS = (
    "security check required",
    "temporarily unavailable",
    "access denied",
    "error reference number",
)


# Browser engine selection
# - This project uses Selenium only (Playwright is not used at runtime).
SELENIUM_BROWSER = os.environ.get("SELENIUM_BROWSER", "chrome").strip().lower()
SELENIUM_HEADLESS = os.environ.get("SELENIUM_HEADLESS")
SELENIUM_DEBUGGER_ADDRESS = os.environ.get("SELENIUM_DEBUGGER_ADDRESS", "").strip()
SELENIUM_FIREFOX_PROFILE_PATH = os.environ.get("SELENIUM_FIREFOX_PROFILE_PATH", "").strip()
SELENIUM_FIREFOX_BINARY = os.environ.get("SELENIUM_FIREFOX_BINARY", "").strip()
SELENIUM_USE_UNDETECTED = os.environ.get("SELENIUM_USE_UNDETECTED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
SELENIUM_USE_STEALTH = os.environ.get("SELENIUM_USE_STEALTH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
SELENIUM_DOWNLOAD_TIMEOUT_SECONDS = float(os.environ.get("SELENIUM_DOWNLOAD_TIMEOUT_SECONDS", "60"))
SELENIUM_PAGELOAD_TIMEOUT_SECONDS = float(os.environ.get("SELENIUM_PAGELOAD_TIMEOUT_SECONDS", "60"))
NODRIVER_WAIT_SECONDS = float(os.environ.get("NODRIVER_WAIT_SECONDS", "2"))
NODRIVER_HOLD_OPEN_SECONDS = float(os.environ.get("NODRIVER_HOLD_OPEN_SECONDS", "0"))
NODRIVER_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("NODRIVER_TOTAL_TIMEOUT_SECONDS", "150"))
NODRIVER_WAF_WAIT_SECONDS = float(os.environ.get("NODRIVER_WAF_WAIT_SECONDS", "15"))
NODRIVER_PDF_DEBUG = os.environ.get("NODRIVER_PDF_DEBUG", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# ------------------------------ Debug Logging ------------------------------


def _pdf_debug(msg: str) -> None:
    """
    Emit verbose PDF-fetch diagnostics via logging.

    When `NODRIVER_PDF_DEBUG=1`, we log at INFO so the messages show up in
    container logs even if the global log level is INFO. Otherwise we log at
    DEBUG to keep noise low in normal runs.
    """
    if NODRIVER_PDF_DEBUG:
        LOGGER.info("%s", msg)
    else:
        LOGGER.debug("%s", msg)

# ------------------------------ Runtime Helpers ------------------------------


def _get_windows_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _WINDOWS_EXECUTOR
    if _WINDOWS_EXECUTOR is None:
        _WINDOWS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="timeout_guard",
        )
        atexit.register(_WINDOWS_EXECUTOR.shutdown, wait=False)
    return _WINDOWS_EXECUTOR


def _selenium_headless() -> bool:
    if SELENIUM_HEADLESS is not None:
        return SELENIUM_HEADLESS.strip().lower() not in ("0", "false", "no")
    # Default: headed on dev machines, headless on Linux servers/CI.
    return platform.system() == "Linux"

# ------------------------------ Tor / Proxy Helpers ------------------------------


def _get_tor_port() -> int | None:
    """Return the Tor SOCKS port for browser-only proxying."""
    global TOR_SOCKS_PORT
    if TOR_SOCKS_PORT is not None:
        return TOR_SOCKS_PORT
    env = os.environ.get("TOR_SOCKS_PORT")
    if env:
        TOR_SOCKS_PORT = int(env)
        return TOR_SOCKS_PORT
    try:
        from tor_proxy import tor_proxy
    except Exception as exc:
        raise RuntimeError(
            "tor-proxy is required for Tor browser routing. "
            "Install with: pip install tor-proxy or set TOR_SOCKS_PORT."
        ) from exc
    TOR_SOCKS_PORT = int(tor_proxy())
    return TOR_SOCKS_PORT


def _chrome_tor_args() -> list[str]:
    port = _get_tor_port()
    if not port:
        return []
    host = "127.0.0.1"
    return [
        f"--proxy-server=socks5://{host}:{port}",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--dns-prefetch-disable",
    ]


def _apply_firefox_tor_prefs(opts_or_profile) -> None:
    port = _get_tor_port()
    if not port:
        return
    setter = getattr(opts_or_profile, "set_preference", None)
    if not callable(setter):
        return
    setter("network.proxy.type", 1)
    setter("network.proxy.socks", "127.0.0.1")
    setter("network.proxy.socks_port", int(port))
    setter("network.proxy.socks_version", 5)
    setter("network.proxy.socks_remote_dns", True)

# ------------------------------ Selenium Driver Setup ------------------------------


def _selenium_available() -> bool:
    try:
        import selenium  # noqa: F401
        return True
    except Exception as exc:
        LOGGER.debug("selenium import failed: %s", exc)
        return False


def _create_chrome_driver(options):
    if SELENIUM_DEBUGGER_ADDRESS:
        from selenium import webdriver

        return webdriver.Chrome(options=options)
    if SELENIUM_USE_UNDETECTED:
        try:
            import undetected_chromedriver as uc  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "undetected-chromedriver is required for SELENIUM_USE_UNDETECTED=1. "
                "Install with: pip install undetected-chromedriver"
            ) from exc
        try:
            return uc.Chrome(options=options, use_subprocess=True)
        except Exception as exc:
            _pdf_debug(f"[Selenium][PDF] undetected_chromedriver failed: {exc} (retrying with Selenium Chrome)")
            from selenium import webdriver

            return webdriver.Chrome(options=options)
    from selenium import webdriver

    return webdriver.Chrome(options=options)


def _apply_selenium_stealth(driver) -> None:
    if not SELENIUM_USE_STEALTH:
        return
    try:
        from selenium_stealth import stealth  # type: ignore
    except Exception as exc:
        LOGGER.debug("selenium-stealth import failed (skipping): %s", exc)
        return
    system = platform.system()
    if system == "Darwin":
        platform_name = "MacIntel"
    elif system == "Linux":
        platform_name = "Linux x86_64"
    else:
        platform_name = "Win32"
    try:
        stealth(
            driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform=platform_name,
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
    except Exception as exc:
        LOGGER.debug("selenium-stealth failed to apply: %s", exc)




def _apply_canvas_spoof_cdp(driver) -> None:
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": CANVAS_SPOOF_SCRIPT},
        )
    except Exception as exc:
        LOGGER.debug("Failed to apply canvas spoof via CDP: %s", exc)


async def _nodriver_apply_canvas_spoof(browser) -> None:
    try:
        cdp = getattr(browser, "cdp", None)
        if cdp is not None:
            page = getattr(cdp, "Page", None)
            add_script = getattr(page, "addScriptToEvaluateOnNewDocument", None)
            if callable(add_script):
                await add_script(source=CANVAS_SPOOF_SCRIPT)
                return
        send = getattr(browser, "send", None)
        if callable(send):
            await send("Page.addScriptToEvaluateOnNewDocument", {"source": CANVAS_SPOOF_SCRIPT})
    except Exception as exc:
        LOGGER.debug("Failed to apply canvas spoof in Nodriver: %s", exc)
        return


async def _nodriver_cdp_call(browser, method: str, params: dict | None = None):
    params = params or {}
    try:
        cdp = getattr(browser, "cdp", None)
        if cdp is not None:
            target = cdp
            for part in method.split("."):
                target = getattr(target, part, None)
                if target is None:
                    break
            if callable(target):
                return await target(**params)
    except Exception as exc:
        LOGGER.debug("CDP call failed (%s): %s", method, exc)
    try:
        send = getattr(browser, "send", None)
        if callable(send):
            return await send(method, params)
    except Exception as exc:
        LOGGER.debug("Browser send() CDP call failed (%s): %s", method, exc)
        return None
    return None


def _nodriver_iter_resources(frame_tree: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if not frame_tree:
        return out
    frame = frame_tree.get("frame") or {}
    frame_id = frame.get("id") or ""
    for res in frame_tree.get("resources") or []:
        out.append((frame_id, res))
    for child in frame_tree.get("childFrames") or []:
        out.extend(_nodriver_iter_resources(child))
    return out


async def _nodriver_sleep(page, seconds: float) -> None:
    sleeper = getattr(page, "sleep", None)
    if callable(sleeper):
        await sleeper(seconds)
    else:
        import asyncio
        await asyncio.sleep(seconds)


async def _nodriver_fetch_content(
    url: str,
    *,
    referer: str | None,
    headless: bool,
) -> object:
    try:
        import nodriver as uc
    except Exception as exc:
        raise RuntimeError(
            "nodriver is required for Chrome automation. Install with: pip install nodriver"
        ) from exc

    browser = await uc.start(
        headless=headless,
        browser_args=[
            "--window-size=1920,1080",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={UA}",
            *_chrome_tor_args(),
        ],
    )
    await _nodriver_apply_canvas_spoof(browser)
    try:
        if referer:
            ref_page = await browser.get(referer)
            await _nodriver_sleep(ref_page, 0.5)
        page = await browser.get(url)
        await _nodriver_sleep(page, NODRIVER_WAIT_SECONDS)
        getter = getattr(page, "get_content", None)
        if callable(getter):
            content = await getter()
        else:
            getter = getattr(page, "content", None)
            if callable(getter):
                content = await getter()
            else:
                content = ""
        if NODRIVER_HOLD_OPEN_SECONDS > 0:
            await _nodriver_sleep(page, NODRIVER_HOLD_OPEN_SECONDS)
        return content
    finally:
        await _nodriver_stop_browser_best_effort(browser, label="content fetch")


async def _nodriver_stop_browser_best_effort(browser, *, label: str) -> None:
    stop = getattr(browser, "stop", None)
    if not callable(stop):
        return
    try:
        import asyncio

        res = stop()
        if asyncio.iscoroutine(res):
            await res
    except Exception as exc:
        LOGGER.debug("Failed to stop Nodriver browser (%s): %s", label, exc)


def _nodriver_run(coro):
    """
    Run an async Nodriver coroutine from synchronous code.

    Key behavior:
    - Ensures we are **not already inside an asyncio event loop**. This helper is for
      sync call sites only. If you're already async, call the underlying async function
      directly instead of nesting loops.
    - Reuses a single event loop per thread. Nodriver's own helpers tend to create a
      fresh loop frequently; in long-running processes this can leak resources.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "_nodriver_run() must not be called from within a running asyncio loop. "
            "Call the underlying async Nodriver function directly instead."
        )

    try:
        import nodriver as uc
    except Exception as exc:
        raise RuntimeError(
            "nodriver is required for Chrome automation. Install with: pip install nodriver"
        ) from exc

    _ = uc  # keep explicit import requirement (and silence "unused" linters)

    loop = getattr(_NODRIVER_LOOP_LOCAL, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _NODRIVER_LOOP_LOCAL.loop = loop
        _NODRIVER_LOOPS.append(loop)
        _install_nodriver_loop_exception_handler(loop)

    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _iter_exception_chain(exc: BaseException | None):
    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        yield cur
        seen.add(id(cur))
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt


def _install_nodriver_loop_exception_handler(loop) -> None:
    if getattr(loop, "_paper_scraper_nodriver_handler_installed", False):
        return
    loop._paper_scraper_nodriver_handler_installed = True  # type: ignore[attr-defined]
    loop.set_exception_handler(_nodriver_loop_exception_handler)


def _should_suppress_nodriver_autodiscover_exception(context: dict) -> bool:
    exc = context.get("exception")
    if not _is_nodriver_connection_refused(exc):
        return False

    task = context.get("future") or context.get("task")
    get_coro = getattr(task, "get_coro", None)
    if not callable(get_coro):
        return False

    coro = get_coro()
    code = getattr(coro, "cr_code", None)
    if code is None:
        return False

    filename = str(getattr(code, "co_filename", "") or "")
    name = str(getattr(code, "co_name", "") or "")
    return "nodriver/core/browser.py" in filename and name == "update_targets"


def _nodriver_loop_exception_handler(loop, context) -> None:
    if _should_suppress_nodriver_autodiscover_exception(context):
        LOGGER.debug(
            "[NodriverLoop] Suppressed expected Nodriver autodiscover task exception: %s",
            context.get("exception"),
        )
        return
    loop.default_exception_handler(context)


def _is_nodriver_stopiteration(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    for item in _iter_exception_chain(exc):
        if isinstance(item, RuntimeError) and "coroutine raised StopIteration" in str(item):
            return True
    return False


def _is_nodriver_connection_refused(exc: BaseException | None) -> bool:
    """
    Detect Nodriver CDP failures where the local websocket connection is refused.

    We intentionally require a localhost hint (127.0.0.1/localhost) to avoid
    treating generic network failures (Tor/proxy/outbound) as CDP failures.
    """
    if exc is None:
        return False

    errno_values = {111, 61, 10061}  # Linux/macOS/Windows common "connection refused" values.
    saw_errno = False
    saw_local = False

    for item in _iter_exception_chain(exc):
        if isinstance(item, OSError) and getattr(item, "errno", None) in errno_values:
            saw_errno = True
            s = str(item)
            if "127.0.0.1" in s or "localhost" in s:
                saw_local = True

    if not saw_errno:
        return False

    if saw_local:
        return True

    low = str(exc).lower()
    return "127.0.0.1" in low or "localhost" in low


def _is_selenium_connection_refused(exc: BaseException | None) -> bool:
    if exc is None:
        return False

    # Prefer checking OS-level errno in the exception chain (portable across wrappers).
    errno_values = {111, 61, 10061}  # Linux/macOS/Windows common "connection refused" values.
    for item in _iter_exception_chain(exc):
        if isinstance(item, OSError) and getattr(item, "errno", None) in errno_values:
            return True

    low = str(exc).lower()
    return (
        "connection refused" in low
        or "errno 111" in low
        or "errno 61" in low
        or "winerror 10061" in low
    )


def _retry_once_on_nodriver_stopiteration(fn, *, on_restart, url: str, label: str):
    try:
        return fn()
    except Exception as exc:
        if not _is_nodriver_stopiteration(exc):
            raise
        LOGGER.warning(
            "[%s] Nodriver StopIteration failure; restarting and retrying once: %s (%s)",
            label,
            url,
            exc,
        )
        try:
            on_restart()
        except Exception as restart_exc:
            LOGGER.warning("[%s] Nodriver restart failed (will not retry): %s", label, restart_exc)
            raise exc
        return fn()


def _retry_once_on_nodriver_connection_refused(fn, *, on_restart, url: str, label: str):
    try:
        return fn()
    except Exception as exc:
        if not _is_nodriver_connection_refused(exc):
            raise
        LOGGER.warning(
            "[%s] Nodriver CDP unreachable (connection refused); restarting and retrying once: %s (%s)",
            label,
            url,
            exc,
        )
        try:
            on_restart()
        except Exception as restart_exc:
            LOGGER.warning("[%s] Nodriver restart failed (will not retry): %s", label, restart_exc)
            raise exc
        return fn()


def _retry_once_on_selenium_connection_refused(fn, *, on_restart, url: str, label: str):
    try:
        return fn()
    except Exception as exc:
        if not _is_selenium_connection_refused(exc):
            raise
        LOGGER.warning(
            "[%s] Selenium driver unreachable (connection refused); restarting and retrying once: %s (%s)",
            label,
            url,
            exc,
        )
        try:
            on_restart()
        except Exception as restart_exc:
            LOGGER.warning("[%s] Selenium restart failed (will not retry): %s", label, restart_exc)
            raise exc
        return fn()

# ------------------------------ Cookie Bridging (Nodriver -> Selenium) ------------------------------


def _cookie_domain_matches(cookie_domain: str, host: str) -> bool:
    if not cookie_domain or not host:
        return False
    domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    return host == domain or host.endswith(f".{domain}")


def _filter_cookies_for_hosts(cookies: list[dict], hosts: set[str]) -> list[dict]:
    if not hosts:
        return list(cookies)
    filtered: list[dict] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if not domain:
            continue
        if any(_cookie_domain_matches(domain, host) for host in hosts):
            filtered.append(cookie)
    return filtered


def _normalize_cookie_for_selenium(cookie: dict) -> dict:
    name = cookie.get("name")
    value = cookie.get("value")
    if not name or value is None:
        return {}
    out: dict = {"name": name, "value": value}
    domain = cookie.get("domain")
    if domain:
        out["domain"] = str(domain)
    path = cookie.get("path")
    if path:
        out["path"] = str(path)
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        out["expiry"] = int(expires)
    same_site = cookie.get("sameSite")
    if isinstance(same_site, str):
        normalized = same_site.strip().lower()
        if normalized in ("lax", "strict", "none"):
            out["sameSite"] = normalized.capitalize() if normalized != "none" else "None"
            if normalized == "none":
                out["secure"] = True
    if "secure" in cookie and "secure" not in out:
        out["secure"] = bool(cookie.get("secure"))
    if "httpOnly" in cookie:
        out["httpOnly"] = bool(cookie.get("httpOnly"))
    return out


def _origin_for_cookie_domain(domain: str, url: str, referer: str | None, secure: bool) -> str:
    domain = domain.lstrip(".")
    for cand in (url, referer):
        if not cand:
            continue
        try:
            parsed = urlsplit(cand)
        except Exception as exc:
            LOGGER.debug("Failed to parse URL while selecting cookie origin (%r): %s", cand, exc)
            continue
        host = parsed.hostname or parsed.netloc
        if host and _cookie_domain_matches(domain, host):
            scheme = parsed.scheme or ("https" if secure else "http")
            return f"{scheme}://{host}"
    scheme = "https" if secure else "http"
    return f"{scheme}://{domain}"


def _selenium_inject_cookies(driver, cookies: list[dict], *, url: str, referer: str | None) -> None:
    if not cookies:
        return
    hosts: set[str] = set()
    for cand in (url, referer):
        if not cand:
            continue
        try:
            host = urlsplit(cand).hostname
        except Exception as exc:
            LOGGER.debug("Failed to parse URL host for cookie injection (%r): %s", cand, exc)
            host = None
        if host:
            hosts.add(host)
    filtered = _filter_cookies_for_hosts(cookies, hosts)
    if NODRIVER_PDF_DEBUG:
        domains = sorted({str(c.get("domain")) for c in filtered if c.get("domain")})
        _pdf_debug(f"[Nodriver][Cookies] Injecting {len(filtered)} cookies into Selenium for {url}")
        if domains:
            _pdf_debug(f"[Nodriver][Cookies] Domains: {', '.join(domains[:6])}")
    if not filtered:
        return
    cookies_by_domain: dict[str, list[dict]] = {}
    for cookie in filtered:
        domain = str(cookie.get("domain") or "")
        if not domain:
            continue
        cookies_by_domain.setdefault(domain, []).append(cookie)
    for domain, bucket in cookies_by_domain.items():
        origin = _origin_for_cookie_domain(
            domain,
            url,
            referer,
            secure=any(bool(c.get("secure")) for c in bucket),
        )
        try:
            driver.get(f"{origin}/")
        except Exception as exc:
            _pdf_debug(f"[Selenium][Cookies] Could not open {origin} to set cookies: {exc}")
            continue
        for cookie in bucket:
            mapped = _normalize_cookie_for_selenium(cookie)
            if not mapped:
                continue
            try:
                driver.add_cookie(mapped)
            except Exception as exc:
                _pdf_debug(
                    f"[Selenium][Cookies] Failed to add cookie {mapped.get('name')} for {domain}: {exc}"
                )
                continue


# ------------------------------ Nodriver PDF Fetch ------------------------------


class NodriverPdfFetchError(Exception):
    """Raised when Nodriver PDF fetching fails but cookies were captured for Selenium fallback."""

    def __init__(self, message: str, *, url: str, cookies: list[dict] | None = None):
        super().__init__(message)
        self.url = url
        self.cookies: list[dict] = list(cookies or [])


async def _nodriver_extract_cookies(browser, *, url: str, referer: str | None) -> list[dict]:
    """Best-effort cookie extraction from an existing Nodriver session."""
    await _nodriver_cdp_call(browser, "Network.enable")

    cookies = None
    payload = await _nodriver_cdp_call(browser, "Network.getAllCookies")
    if payload and isinstance(payload, dict):
        cookies = payload.get("cookies")

    if not cookies:
        payload = await _nodriver_cdp_call(
            browser,
            "Network.getCookies",
            {"urls": [u for u in (referer, url) if u]},
        )
        if payload and isinstance(payload, dict):
            cookies = payload.get("cookies")

    if not cookies:
        payload = await _nodriver_cdp_call(browser, "Storage.getCookies", {"urls": [url]})
        if payload and isinstance(payload, dict):
            cookies = payload.get("cookies")

    out = list(cookies or [])
    if NODRIVER_PDF_DEBUG:
        domains = sorted({str(c.get("domain")) for c in out if c.get("domain")})
        _pdf_debug(f"[Nodriver][Cookies] Extracted {len(out)} cookies for {url}")
        if domains:
            _pdf_debug(f"[Nodriver][Cookies] Domains: {', '.join(domains[:6])}")
        if not out:
            _pdf_debug(f"[Nodriver][Cookies] No cookies captured for {url}")
    return out


def _nodriver_fetch_cookies(url: str, *, referer: str | None) -> list[dict]:
    async def _fetch() -> list[dict]:
        try:
            import nodriver as uc
        except Exception as exc:
            raise RuntimeError(
                "nodriver is required for Chrome automation. Install with: pip install nodriver"
            ) from exc
        browser = await uc.start(
            headless=_selenium_headless(),
            browser_args=[
                "--window-size=1920,1080",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                f"--user-agent={UA}",
                *_chrome_tor_args(),
            ],
        )
        await _nodriver_apply_canvas_spoof(browser)
        try:
            if referer:
                ref_page = await browser.get(referer)
                await _nodriver_sleep(ref_page, 0.5)
            page = await browser.get(url)
            await _nodriver_sleep(page, NODRIVER_WAIT_SECONDS)
            return await _nodriver_extract_cookies(browser, url=url, referer=referer)
        finally:
            await _nodriver_stop_browser_best_effort(browser, label="cookies fetch")

    return _nodriver_run(_fetch())


def _nodriver_fetch_html(url: str, *, referer: str | None) -> str:
    content = _nodriver_run(
        _nodriver_fetch_content(url, referer=referer, headless=_selenium_headless())
    )
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception as exc:
            LOGGER.debug("Failed to decode Nodriver HTML bytes for %s: %s", url, exc)
            return ""
    return str(content or "")


def _extract_pdf_url_from_chrome_viewer_url(viewer_url: str) -> str | None:
    """
    Extract the underlying PDF URL from Chrome's built-in PDF viewer URLs.

    Example:
      chrome-extension://.../index.html?file=https%3A%2F%2Fexample.com%2Fpaper.pdf

    Notes:
    - This is a best-effort helper and intentionally ignores non-http(s) values.
    - Some variants encode the `file=` value multiple times; we unquote a few times.
    """
    if not viewer_url:
        return None
    low = viewer_url.strip().lower()
    if not (low.startswith("chrome-extension://") or low.startswith("chrome://")):
        return None
    try:
        from urllib.parse import parse_qs, unquote, urlsplit

        parts = urlsplit(viewer_url.strip())
        for raw in (parts.query, parts.fragment):
            if not raw:
                continue
            qs = parse_qs(raw)
            val = (qs.get("file") or qs.get("src") or [None])[0]
            if not val:
                continue
            decoded = str(val).strip()
            for _ in range(3):
                try:
                    decoded2 = unquote(decoded)
                except Exception as exc:
                    LOGGER.debug("Failed to unquote Chrome viewer URL: %s", exc)
                    break
                if decoded2 == decoded:
                    break
                decoded = decoded2
            decoded = decoded.strip()
            if decoded.startswith(("http://", "https://")):
                return decoded
    except Exception as exc:
        LOGGER.debug("Failed to extract PDF URL from Chrome viewer URL %r: %s", viewer_url, exc)
        return None
    return None


async def _nodriver_fetch_pdf_bytes_and_cookies_impl(
    url: str,
    *,
    referer: str | None,
    capture_cookies_for_selenium: bool,
    wrap_errors: bool,
) -> tuple[bytes, str, list[dict]]:
    try:
        import nodriver as uc
    except Exception as exc:
        raise RuntimeError(
            "nodriver is required for Chrome automation. Install with: pip install nodriver"
        ) from exc

    browser = await uc.start(
        headless=_selenium_headless(),
        browser_args=[
            "--window-size=1920,1080",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={UA}",
            *_chrome_tor_args(),
        ],
    )
    await _nodriver_apply_canvas_spoof(browser)
    await _nodriver_cdp_call(browser, "Network.enable")
    try:
        return await _nodriver_fetch_pdf_bytes_and_cookies_impl_with_browser(
            browser,
            url,
            referer=referer,
            capture_cookies_for_selenium=capture_cookies_for_selenium,
            wrap_errors=wrap_errors,
        )
    finally:
        await _nodriver_stop_browser_best_effort(browser, label="pdf bytes")


async def _nodriver_close_page_best_effort(page) -> None:
    if page is None:
        return
    closer = getattr(page, "close", None)
    if not callable(closer):
        return
    try:
        import asyncio

        res = closer()
        if asyncio.iscoroutine(res):
            await res
    except Exception as exc:
        _pdf_debug(f"[Nodriver][PDF] page.close failed: {exc}")


async def _nodriver_fetch_pdf_bytes_and_cookies_impl_with_browser(
    browser,
    url: str,
    *,
    referer: str | None,
    capture_cookies_for_selenium: bool,
    wrap_errors: bool,
) -> tuple[bytes, str, list[dict]]:
    """
    Fetch a PDF via an existing Nodriver browser instance.

    This function is the core of the "Nodriver-first" PDF strategy. It tries multiple
    techniques in a *single* browser session, and (optionally) captures cookies that
    can be injected into Selenium for a second-chance download.

    High-level strategy (roughly in order):
    1) Allow Chrome downloads to a temp directory and try to detect a completed PDF file.
       - We only wait "long" if we see evidence that an actual download started
         (e.g., a `.crdownload` temp file exists).
    2) Attempt a `fetch()` in the page context to retrieve the PDF bytes (may fail due to CORS).
    3) Attempt to read PDF bytes from Chrome's resource tree (CDP).
    4) As a last resort, call `Page.printToPDF` (CDP). This can produce a PDF of the rendered
       page, which is sometimes not the original PDF file. When we use this method and
       `capture_cookies_for_selenium=True`, we capture cookies so Selenium can retry later
       to get the *real* PDF if possible.
    5) Rare fallback: read the page content bytes if Nodriver returns raw bytes.

    Returns:
      (pdf_bytes, method, cookies)
      - `method` is a short string describing which strategy produced the bytes.
      - `cookies` are only returned when explicitly requested (for Selenium fallback).
    """
    try:
        import tempfile
        from pathlib import Path
        import time

        url = (url or "").strip()
        low_url = url.lower()
        if not url or low_url in ("undefined", "null", "none", "about:blank"):
            raise ValueError(f"Invalid PDF URL: {url!r}")
        if not low_url.startswith(("http://", "https://")):
            raise ValueError(f"Unsupported URL scheme for PDF fetch: {url!r}")

        ignore_cleanup_errors = os.name == "nt"
        with tempfile.TemporaryDirectory(
            prefix="nodriver-pdf-",
            ignore_cleanup_errors=ignore_cleanup_errors,
        ) as td:
            download_dir = Path(td)
            download_behavior = {"behavior": "allow", "downloadPath": str(download_dir)}
            try:
                await _nodriver_cdp_call(browser, "Page.setDownloadBehavior", download_behavior)
            except Exception as exc:
                _pdf_debug(f"[Nodriver][PDF] setDownloadBehavior failed: {exc}")

            ref_page = None
            page = None
            try:
                if referer:
                    ref_page = await browser.get(referer)
                    await _nodriver_sleep(ref_page, 0.5)

                page = await browser.get(url)
                await _nodriver_sleep(page, NODRIVER_WAIT_SECONDS)

                async def _cdp(method: str, params: dict | None = None):
                    """Prefer page-scoped CDP calls, fall back to browser-scoped."""
                    res = await _nodriver_cdp_call(page, method, params)
                    if res is not None:
                        return res
                    return await _nodriver_cdp_call(browser, method, params)

                async def _inspect_document() -> tuple[str, str, str]:
                    """Return (href, content_type, title) for the current page (best-effort)."""
                    try:
                        eval_res = await _cdp(
                            "Runtime.evaluate",
                            {
                                "expression": "(function(){return {href: location.href, ct: (document.contentType||''), title: (document.title||'')}})()",
                                "returnByValue": True,
                            },
                        )
                        payload = (
                            (eval_res or {}).get("result") if isinstance(eval_res, dict) else None
                        )
                        value = (payload or {}).get("value") if isinstance(payload, dict) else None
                        if not isinstance(value, dict):
                            return "", "", ""
                        href = str(value.get("href") or "")
                        ct = str(value.get("ct") or "")
                        title = str(value.get("title") or "")
                        return href, ct, title
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][PDF] Could not inspect location/contentType: {exc}")
                        return "", "", ""

                real_pdf_url = url
                href_now, _ct_now, title_now = await _inspect_document()
                if href_now:
                    extracted = _extract_pdf_url_from_chrome_viewer_url(href_now)
                    if extracted:
                        real_pdf_url = extracted

                async def _capture_cookies_best_effort() -> list[dict]:
                    try:
                        return await _nodriver_extract_cookies(browser, url=url, referer=referer)
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][Cookies] Cookie capture failed: {exc}")
                        return []

                try:
                    # Some WAF/Cloudflare pages clear after a short delay; wait briefly before giving up.
                    if NODRIVER_WAF_WAIT_SECONDS > 0:
                        async def _waf_snapshot() -> tuple[str, str]:
                            try:
                                eval_res = await _cdp(
                                    "Runtime.evaluate",
                                    {
                                        "expression": (
                                            "(function(){"
                                            "const t=(document.title||'');"
                                            "const body=(document.body&&document.body.innerText)||'';"
                                            "return {title:t, text: body.slice(0, 6000)};"
                                            "})()"
                                        ),
                                        "returnByValue": True,
                                    },
                                )
                                payload = (
                                    (eval_res or {}).get("result")
                                    if isinstance(eval_res, dict)
                                    else None
                                )
                                value = (
                                    (payload or {}).get("value") if isinstance(payload, dict) else None
                                )
                                if not isinstance(value, dict):
                                    return "", ""
                                return str(value.get("text") or ""), str(value.get("title") or "")
                            except Exception as exc:
                                _pdf_debug(f"[Nodriver][PDF] WAF snapshot failed: {exc}")
                                return "", ""

                        html_now, snap_title = await _waf_snapshot()
                        if not snap_title:
                            snap_title = title_now
                        if _looks_like_bot_check_page(html_now, snap_title):
                            deadline = time.monotonic() + float(NODRIVER_WAF_WAIT_SECONDS)
                            _pdf_debug(
                                f"[Nodriver][PDF] Detected WAF/CAPTCHA page; waiting up to {NODRIVER_WAF_WAIT_SECONDS}s: {url}"
                            )
                            cleared = False
                            while time.monotonic() < deadline:
                                await _nodriver_sleep(page, 1.0)
                                html_now, snap_title = await _waf_snapshot()
                                if _looks_like_bot_check_page(html_now, snap_title):
                                    continue
                                cleared = True
                                break
                            # Refresh inspected URL/title after potential redirect.
                            href_now, _ct_now, title_now = await _inspect_document()
                            if href_now:
                                extracted = _extract_pdf_url_from_chrome_viewer_url(href_now)
                                if extracted:
                                    real_pdf_url = extracted
                            if not cleared and _looks_like_bot_check_page(
                                html_now, snap_title or title_now
                            ):
                                _pdf_debug(
                                    f"[Nodriver][PDF] WAF/CAPTCHA did not clear after wait; failing fast: {url}"
                                )
                                raise FetchBlocked("WAF/CAPTCHA interstitial (did not clear)")

                    def _has_crdownload_now() -> bool:
                        try:
                            for p in download_dir.iterdir():
                                if p.is_file() and p.name.endswith(".crdownload"):
                                    return True
                        except Exception as exc:
                            LOGGER.debug("Download dir scan failed while checking .crdownload: %s", exc)
                            return False
                        return False

                    async def _wait_for_download_pdf() -> bytes | None:
                        """Wait for a stable downloaded file and return PDF bytes if found."""
                        deadline = time.monotonic() + float(SELENIUM_DOWNLOAD_TIMEOUT_SECONDS)
                        last_file: Path | None = None
                        last_size: int | None = None
                        stable_count = 0
                        while time.monotonic() < deadline:
                            candidates = [
                                p
                                for p in download_dir.iterdir()
                                if p.is_file() and not p.name.endswith(".crdownload")
                            ]
                            if candidates:
                                target = max(candidates, key=lambda p: p.stat().st_size)
                                try:
                                    size = int(target.stat().st_size)
                                except Exception as exc:
                                    LOGGER.debug("Failed to stat download candidate %s: %s", target, exc)
                                    size = 0
                                if last_file is None or target != last_file:
                                    last_file = target
                                    last_size = None
                                    stable_count = 0
                                if size > 0 and size == last_size:
                                    stable_count += 1
                                    if stable_count >= 3:
                                        # Only accept a completed download when no .crdownload remains.
                                        if _has_crdownload_now():
                                            stable_count = 0
                                            await _nodriver_sleep(page, 0.5)
                                            continue
                                        data = b""
                                        for retry in range(3):
                                            try:
                                                data = target.read_bytes()
                                                break
                                            except PermissionError:
                                                await _nodriver_sleep(page, 0.1 * (2**retry))
                                        if _looks_like_pdf_bytes(data):
                                            return data
                                else:
                                    last_size = size
                                    stable_count = 0
                            await _nodriver_sleep(page, 0.5)
                        return None

                    # 1) Capture an actual download.
                    # Only wait a long time if a Chrome download actually started (".crdownload" exists).
                    # Otherwise (inline viewer / no download), proceed quickly to js_fetch/resource fallbacks.
                    has_crdownload = False
                    try:
                        for p in download_dir.iterdir():
                            if p.is_file() and p.name.endswith(".crdownload"):
                                has_crdownload = True
                                break
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][PDF] Download dir scan failed: {exc}")
                        has_crdownload = False

                    # Quick check: sometimes the download completes fast and no ".crdownload" is visible.
                    try:
                        completed = [
                            p
                            for p in download_dir.iterdir()
                            if p.is_file() and not p.name.endswith(".crdownload")
                        ]
                    except Exception as exc:
                        LOGGER.debug("Failed to scan download dir for completed files: %s", exc)
                        completed = []
                    if completed and not _has_crdownload_now():
                        target = max(completed, key=lambda p: p.stat().st_size)
                        try:
                            size1 = int(target.stat().st_size)
                            if size1 > 0:
                                # Give Chrome a brief moment to finish flushing buffers for very fast downloads.
                                await _nodriver_sleep(page, 0.25)
                                size2 = int(target.stat().st_size)
                                if size2 != size1:
                                    size1 = 0
                            data = target.read_bytes() if size1 > 0 else b""
                        except Exception as exc:
                            LOGGER.debug("Failed to read fast-completed download candidate %s: %s", target, exc)
                            data = b""
                        if _looks_like_pdf_bytes(data) and not _has_crdownload_now():
                            _pdf_debug(f"[Nodriver][PDF] Saved via download: {url}")
                            return data, "download", []

                    if has_crdownload:
                        data = await _wait_for_download_pdf()
                        if _looks_like_pdf_bytes(data or b""):
                            _pdf_debug(f"[Nodriver][PDF] Saved via download: {url}")
                            return data, "download", []
                    else:
                        _pdf_debug(f"[Nodriver][PDF] No .crdownload detected; skipping long download wait: {url}")

                    # 2) JS fetch in the page context (may fail due to CORS).
                    try:
                        js_fetch = (
                            "(async function() {"
                            "try {"
                            f"  const target = {json.dumps(real_pdf_url)};"
                            "  const resp = await fetch(target, {credentials: 'include'});"
                            "  if (!resp || !resp.ok) { return null; }"
                            "  const ct = (resp.headers.get('content-type') || '').toLowerCase();"
                            "  if (ct && !(ct.includes('pdf') || ct.includes('octet-stream') || ct.includes('binary'))) { return null; }"
                            "  const blob = await resp.blob();"
                            f"  if (blob && blob.size && blob.size > {MAX_PDF}) {{ return null; }}"
                            "  const b64 = await new Promise((resolve) => {"
                            "    const reader = new FileReader();"
                            "    reader.onloadend = () => {"
                            "      const res = reader.result || '';"
                            "      const idx = res.indexOf(',');"
                            "      resolve(idx >= 0 ? res.slice(idx + 1) : null);"
                            "    };"
                            "    reader.onerror = () => resolve(null);"
                            "    reader.readAsDataURL(blob);"
                            "  });"
                            "  return b64 || null;"
                            "} catch (e) { return null; }"
                            "})();"
                        )
                        result = await _cdp(
                            "Runtime.evaluate",
                            {"expression": js_fetch, "awaitPromise": True, "returnByValue": True},
                        )
                        if result and isinstance(result, dict):
                            payload = result.get("result") or {}
                            b64_val = payload.get("value")
                            if b64_val:
                                data = base64.b64decode(b64_val)
                                if _looks_like_pdf_bytes(data):
                                    _pdf_debug(f"[Nodriver][PDF] Saved via js_fetch: {url}")
                                    return data, "js_fetch", []
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][PDF] js_fetch failed: {exc}")

                    # Late download start: if a download began after our initial scan, wait now.
                    if not has_crdownload:
                        has_crdownload_late = False
                        try:
                            for p in download_dir.iterdir():
                                if p.is_file() and p.name.endswith(".crdownload"):
                                    has_crdownload_late = True
                                    break
                        except Exception as exc:
                            _pdf_debug(f"[Nodriver][PDF] Download dir scan failed: {exc}")
                            has_crdownload_late = False
                        if has_crdownload_late:
                            _pdf_debug(f"[Nodriver][PDF] .crdownload detected after js_fetch; waiting for download: {url}")
                            data = await _wait_for_download_pdf()
                            if _looks_like_pdf_bytes(data or b""):
                                _pdf_debug(f"[Nodriver][PDF] Saved via download: {url}")
                                return data, "download", []

                    # 3) CDP resource extraction (works when PDF is rendered in the viewer).
                    tree = await _cdp("Page.getResourceTree")
                    target_urls: list[tuple[str, str]] = []
                    if tree and isinstance(tree, dict):
                        frame_tree = tree.get("frameTree") or {}
                        for frame_id, res in _nodriver_iter_resources(frame_tree):
                            res_url = (res.get("url") or "").strip()
                            mime_type = (res.get("mimeType") or "").lower()
                            content_size = res.get("contentSize")
                            if isinstance(content_size, (int, float)) and content_size > MAX_PDF:
                                _pdf_debug(
                                    f"[Nodriver][PDF] Skipping oversized resource {res_url} ({int(content_size)} bytes)"
                                )
                                continue
                            if not res_url:
                                continue
                            if (
                                "application/pdf" in mime_type
                                or res_url == real_pdf_url
                                or res_url == url
                                or res_url.lower().endswith(".pdf")
                            ):
                                target_urls.append((frame_id, res_url))

                    target_urls.sort(
                        key=lambda item: (
                            item[1] not in (real_pdf_url, url),
                            item[1] != real_pdf_url,
                            item[1] != url,
                            not item[1].lower().endswith(".pdf"),
                        )
                    )
                    for frame_id, res_url in target_urls:
                        payload = await _cdp(
                            "Page.getResourceContent",
                            {"frameId": frame_id, "url": res_url},
                        )
                        if not payload or not isinstance(payload, dict):
                            continue
                        body = payload.get("content") or ""
                        if payload.get("base64Encoded"):
                            try:
                                data = base64.b64decode(body)
                            except Exception as exc:
                                LOGGER.debug("Failed to decode base64 resource body for %s: %s", res_url, exc)
                                data = b""
                        else:
                            data = str(body).encode("utf-8", errors="ignore")
                        if _looks_like_pdf_bytes(data):
                            _pdf_debug(f"[Nodriver][PDF] Saved via resource: {res_url}")
                            return data, "resource", []

                    if not target_urls:
                        _pdf_debug(f"[Nodriver][PDF] No PDF-like resources in resource tree for: {url}")

                    # 4) Last-resort: print the rendered page to PDF.
                    try:
                        try:
                            getter = getattr(page, "get_content", None)
                            if callable(getter):
                                html = await getter()
                            else:
                                getter = getattr(page, "content", None)
                                html = await getter() if callable(getter) else ""
                        except Exception as exc:
                            _pdf_debug(f"[Nodriver][PDF] Could not read page HTML for WAF check: {exc}")
                            html = ""

                        title_now = ""
                        try:
                            title_eval = await _cdp(
                                "Runtime.evaluate",
                                {"expression": "document.title || ''", "returnByValue": True},
                            )
                            payload = (
                                (title_eval or {}).get("result")
                                if isinstance(title_eval, dict)
                                else None
                            )
                            title_now = (
                                (payload or {}).get("value", "")
                                if isinstance(payload, dict)
                                else ""
                            )
                        except Exception as exc:
                            LOGGER.debug("Failed to evaluate document.title via CDP: %s", exc)
                            title_now = ""

                        if _looks_like_bot_check_page(str(html or ""), str(title_now or "")):
                            _pdf_debug(f"[Nodriver][PDF] Detected WAF/CAPTCHA page; refusing printToPDF: {url}")
                            raise FetchBlocked("WAF/CAPTCHA interstitial")

                        print_res = await _cdp(
                            "Page.printToPDF",
                            {"printBackground": True, "preferCSSPageSize": True},
                        )
                        if print_res and isinstance(print_res, dict):
                            b64_pdf = print_res.get("data")
                            if b64_pdf:
                                data = base64.b64decode(b64_pdf)
                                if _looks_like_pdf_bytes(data):
                                    _pdf_debug(f"[Nodriver][PDF] Saved via printToPDF: {url}")
                                    cookies = []
                                    if capture_cookies_for_selenium:
                                        cookies = await _capture_cookies_best_effort()
                                    return data, "printToPDF", cookies
                    except FetchBlocked:
                        raise
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][PDF] printToPDF failed: {exc}")

                    # 5) Fallback: page content if nodriver returned bytes (rare).
                    try:
                        getter = getattr(page, "get_content", None)
                        if callable(getter):
                            content = await getter()
                        else:
                            getter = getattr(page, "content", None)
                            if callable(getter):
                                content = await getter()
                            else:
                                content = b""
                    except Exception as exc:
                        _pdf_debug(f"[Nodriver][PDF] Could not read page content: {exc}")
                        content = b""

                    if isinstance(content, (bytes, bytearray)):
                        data = bytes(content)
                    else:
                        data = str(content or "").encode("utf-8", errors="ignore")
                    if _looks_like_pdf_bytes(data):
                        _pdf_debug(f"[Nodriver][PDF] Saved via page_content: {url}")
                        return data, "page_content", []

                    raise FetchBlocked("Nodriver did not return PDF bytes")
                except Exception as exc:
                    if capture_cookies_for_selenium and wrap_errors:
                        cookies = await _capture_cookies_best_effort()
                        raise NodriverPdfFetchError(str(exc), url=url, cookies=cookies) from exc
                    raise
            finally:
                if NODRIVER_HOLD_OPEN_SECONDS > 0 and page is not None:
                    await _nodriver_sleep(page, NODRIVER_HOLD_OPEN_SECONDS)
                await _nodriver_close_page_best_effort(page)
                await _nodriver_close_page_best_effort(ref_page)
    except Exception as exc:
        LOGGER.debug("Nodriver PDF fetch failed (url=%s): %s", url, exc)
        raise


def _nodriver_fetch_pdf_bytes_and_cookies(url: str, *, referer: str | None) -> tuple[bytes, str, list[dict]]:
    async def _fetch_all() -> tuple[bytes, str, list[dict]]:
        import asyncio

        return await asyncio.wait_for(
            _nodriver_fetch_pdf_bytes_and_cookies_impl(
                url,
                referer=referer,
                capture_cookies_for_selenium=True,
                wrap_errors=True,
            ),
            timeout=NODRIVER_TOTAL_TIMEOUT_SECONDS,
        )

    return _retry_once_on_nodriver_stopiteration(
        lambda: _nodriver_run(_fetch_all()),
        on_restart=lambda: None,
        url=url,
        label="Nodriver",
    )


def _nodriver_fetch_pdf_bytes(url: str, *, referer: str | None) -> tuple[bytes, str]:
    async def _fetch_bytes() -> tuple[bytes, str, list[dict]]:
        import asyncio

        return await asyncio.wait_for(
            _nodriver_fetch_pdf_bytes_and_cookies_impl(
                url,
                referer=referer,
                capture_cookies_for_selenium=False,
                wrap_errors=False,
            ),
            timeout=NODRIVER_TOTAL_TIMEOUT_SECONDS,
        )

    data, method, _cookies = _retry_once_on_nodriver_stopiteration(
        lambda: _nodriver_run(_fetch_bytes()),
        on_restart=lambda: None,
        url=url,
        label="Nodriver",
    )
    return data, method


# ------------------------------ Persistent Browser Sessions ------------------------------


class PersistentBrowserSessions:
    """
    Keep long-lived browser sessions across many DOIs to reduce startup overhead.

    This is intentionally a lightweight, sync-friendly wrapper around Nodriver's
    async APIs. It is designed for sequential batch pipelines (like
    ingestion/scraping jobs that repeatedly fetch many related URLs.

    Notes:
    - Nodriver is used as the **primary** browser session, because it tends to behave
      more like a real user browser (and can capture cookies for Selenium fallback).
    - Selenium can optionally be kept persistent too (`IPK_PERSISTENT_SELENIUM=1`),
      but only for **sequential** usage (not safe for concurrent callers).
    """

    def __init__(
        self,
        *,
        restart_every_dois: int | None = None,
        persistent_selenium: bool | None = None,
    ) -> None:
        import atexit

        if restart_every_dois is None:
            try:
                restart_every_dois = int(os.environ.get("IPK_BROWSER_RESTART_EVERY_DOIS", "50"))
            except Exception as exc:
                LOGGER.debug("Invalid IPK_BROWSER_RESTART_EVERY_DOIS value: %s", exc)
                restart_every_dois = 50
        if restart_every_dois is None or restart_every_dois < 0:
            restart_every_dois = 0

        if persistent_selenium is None:
            persistent_selenium = os.environ.get("IPK_PERSISTENT_SELENIUM", "0").strip().lower() in (
                "1",
                "true",
                "yes",
            )

        self.restart_every_dois = int(restart_every_dois)
        self.persistent_selenium = bool(persistent_selenium)

        self._dois_since_restart = 0
        self._nodriver_browser = None
        self._selenium_driver = None
        self._selenium_download_root = None
        self._closed = False

        # Best-effort cleanup on interpreter exit (script still closes explicitly).
        atexit.register(self.close)

    def start_doi(self, doi: str | None = None, *, idx: int | None = None) -> None:
        """Call exactly once per processed DOI (after skip checks)."""
        if self._closed:
            raise RuntimeError("PersistentBrowserSessions is closed")

        if self.restart_every_dois > 0 and self._dois_since_restart >= self.restart_every_dois:
            suffix = ""
            if idx is not None:
                suffix += f" idx={idx}"
            if doi:
                suffix += f" doi={doi}"
            LOGGER.info(
                "[BrowserSessions] Restarting browsers after %s DOIs%s",
                self._dois_since_restart,
                suffix,
            )
            self.restart()

        self._dois_since_restart += 1

    def restart(self) -> None:
        """Force restart of persistent sessions and reset DOI counter."""
        self._dois_since_restart = 0
        self._stop_nodriver_best_effort()
        if self.persistent_selenium:
            self._stop_selenium_best_effort()

    def close(self) -> None:
        """Stop any persistent browsers (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._dois_since_restart = 0
        self._stop_nodriver_best_effort()
        self._stop_selenium_best_effort()

    async def _ensure_nodriver_started_async(self):
        if self._nodriver_browser is not None:
            # Quick health check (fails if the browser died).
            try:
                await _nodriver_cdp_call(self._nodriver_browser, "Browser.getVersion")
                return self._nodriver_browser
            except Exception as exc:
                LOGGER.warning("[BrowserSessions] Nodriver browser unhealthy; restarting: %s", exc)
                await self._stop_nodriver_async()

        try:
            import nodriver as uc
        except Exception as exc:
            raise RuntimeError(
                "nodriver is required for Chrome automation. Install with: pip install nodriver"
            ) from exc

        browser = await uc.start(
            headless=_selenium_headless(),
            browser_args=[
                "--window-size=1920,1080",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                f"--user-agent={UA}",
                *_chrome_tor_args(),
            ],
        )
        await _nodriver_apply_canvas_spoof(browser)
        await _nodriver_cdp_call(browser, "Network.enable")
        self._nodriver_browser = browser
        LOGGER.info("[BrowserSessions] Started persistent Nodriver browser")
        return browser

    async def _stop_nodriver_async(self) -> None:
        browser = self._nodriver_browser
        self._nodriver_browser = None
        if browser is None:
            return
        await _nodriver_stop_browser_best_effort(browser, label="persistent stop")

    def _stop_nodriver_best_effort(self) -> None:
        if self._nodriver_browser is None:
            return

        async def _stop() -> None:
            try:
                await self._stop_nodriver_async()
            except Exception as exc:
                LOGGER.warning("[BrowserSessions] Failed to stop Nodriver browser: %s", exc)

        _nodriver_run(_stop())

    def _stop_selenium_best_effort(self) -> None:
        driver = self._selenium_driver
        self._selenium_driver = None
        root = self._selenium_download_root
        self._selenium_download_root = None
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                LOGGER.debug("Failed to quit Selenium driver: %s", exc)
        if root:
            try:
                import shutil
                import time

                for retry in range(3):
                    try:
                        shutil.rmtree(root, ignore_errors=False)
                        break
                    except PermissionError:
                        time.sleep(0.2 * (2**retry))
            except Exception as exc:
                LOGGER.warning("[BrowserSessions] Selenium temp dir cleanup failed: %s", exc)

    def acquire_chrome_selenium_driver(self):
        """
        Return a persistent Chrome Selenium driver if enabled, else None.

        NOTE: This is best-effort and intended for sequential batch use only.
        """
        if not self.persistent_selenium:
            return None
        if SELENIUM_DEBUGGER_ADDRESS:
            # If user attached Selenium to an existing Chrome, we don't manage lifecycle.
            return None

        def _healthy(drv) -> bool:
            try:
                drv.execute_script("return 1")
                return True
            except Exception as exc:
                LOGGER.debug("Selenium driver health check failed: %s", exc)
                return False

        if self._selenium_driver is not None and _healthy(self._selenium_driver):
            return self._selenium_driver

        # Replace unhealthy driver.
        self._stop_selenium_best_effort()

        try:
            import tempfile
            from selenium.webdriver.chrome.options import Options as ChromeOptions
        except Exception as exc:
            raise RuntimeError("selenium is required for Selenium fallback") from exc

        if self._selenium_download_root is None:
            self._selenium_download_root = tempfile.mkdtemp(prefix="selenium-persistent-")

        opts = ChromeOptions()
        if not SELENIUM_DEBUGGER_ADDRESS and _selenium_headless():
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--user-agent={UA}")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        for arg in _chrome_tor_args():
            opts.add_argument(arg)

        prefs = {
            "download.default_directory": str(self._selenium_download_root),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "profile.default_content_settings.popups": 0,
            "plugins.always_open_pdf_externally": True,
        }
        opts.add_experimental_option("prefs", prefs)

        driver = _create_chrome_driver(opts)
        _apply_canvas_spoof_cdp(driver)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
            )
        except Exception as exc:
            LOGGER.debug("Failed to apply navigator.webdriver patch via CDP: %s", exc)
        _apply_selenium_stealth(driver)
        self._selenium_driver = driver
        LOGGER.info("[BrowserSessions] Started persistent Selenium Chrome driver")
        return driver

    def nodriver_fetch_pdf_bytes_and_cookies(
        self,
        url: str,
        *,
        referer: str | None,
    ) -> tuple[bytes, str, list[dict]]:
        """Fetch PDF bytes via the persistent Nodriver browser (with cookies for Selenium fallback)."""

        async def _fetch_all() -> tuple[bytes, str, list[dict]]:
            import asyncio

            browser = await self._ensure_nodriver_started_async()
            try:
                return await asyncio.wait_for(
                    _nodriver_fetch_pdf_bytes_and_cookies_impl_with_browser(
                        browser,
                        url,
                        referer=referer,
                        capture_cookies_for_selenium=True,
                        wrap_errors=True,
                    ),
                    timeout=NODRIVER_TOTAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                cookies: list[dict] = []
                try:
                    cookies = await _nodriver_extract_cookies(browser, url=url, referer=referer)
                except Exception as cookie_exc:
                    _pdf_debug(f"[BrowserSessions] Timeout cookie capture failed: {cookie_exc}")
                    cookies = []
                # Restart Nodriver to avoid accumulating stuck tabs/state after a hard timeout.
                try:
                    await self._stop_nodriver_async()
                except Exception as stop_exc:
                    LOGGER.debug("Failed to stop Nodriver after timeout: %s", stop_exc)
                raise NodriverPdfFetchError(
                    f"Nodriver timed out after {NODRIVER_TOTAL_TIMEOUT_SECONDS}s",
                    url=url,
                    cookies=cookies,
                ) from exc

        _pdf_debug(f"[BrowserSessions] Nodriver fetch (persistent): {url}")
        return _retry_once_on_nodriver_connection_refused(
            lambda: _retry_once_on_nodriver_stopiteration(
                lambda: _nodriver_run(_fetch_all()),
                on_restart=self._stop_nodriver_best_effort,
                url=url,
                label="BrowserSessions",
            ),
            on_restart=self._stop_nodriver_best_effort,
            url=url,
            label="BrowserSessions",
        )

    def nodriver_fetch_pdf_bytes(
        self,
        url: str,
        *,
        referer: str | None,
    ) -> tuple[bytes, str]:
        async def _fetch_bytes() -> tuple[bytes, str, list[dict]]:
            import asyncio

            browser = await self._ensure_nodriver_started_async()
            return await asyncio.wait_for(
                _nodriver_fetch_pdf_bytes_and_cookies_impl_with_browser(
                    browser,
                    url,
                    referer=referer,
                    capture_cookies_for_selenium=False,
                    wrap_errors=False,
                ),
                timeout=NODRIVER_TOTAL_TIMEOUT_SECONDS,
            )

        data, method, _cookies = _retry_once_on_nodriver_connection_refused(
            lambda: _retry_once_on_nodriver_stopiteration(
                lambda: _nodriver_run(_fetch_bytes()),
                on_restart=self._stop_nodriver_best_effort,
                url=url,
                label="BrowserSessions",
            ),
            on_restart=self._stop_nodriver_best_effort,
            url=url,
            label="BrowserSessions",
        )
        return data, method


# ------------------------------ Selenium CDP Helpers (PDF) ------------------------------


def _selenium_cdp_try_resource_pdf(driver, url: str) -> tuple[bytes | None, str]:
    try:
        tree = driver.execute_cdp_cmd("Page.getResourceTree", {})
    except Exception as exc:
        LOGGER.debug("CDP getResourceTree failed: %s", exc)
        return None, "resource"
    target_urls: list[tuple[str, str]] = []
    if tree and isinstance(tree, dict):
        frame_tree = tree.get("frameTree") or {}
        for frame_id, res in _nodriver_iter_resources(frame_tree):
            res_url = (res.get("url") or "").strip()
            mime_type = (res.get("mimeType") or "").lower()
            res_type = (res.get("type") or "").strip()
            if not res_url:
                continue
            if (
                "application/pdf" in mime_type
                or res_url == url
                or res_url.lower().endswith(".pdf")
                or res_type == "Document"
            ):
                target_urls.append((frame_id, res_url))
    target_urls.sort(key=lambda item: (item[1] != url, not item[1].lower().endswith(".pdf")))
    for frame_id, res_url in target_urls:
        try:
            payload = driver.execute_cdp_cmd(
                "Page.getResourceContent",
                {"frameId": frame_id, "url": res_url},
            )
        except Exception as exc:
            LOGGER.debug("CDP getResourceContent failed for %s: %s", res_url, exc)
            continue
        if not payload or not isinstance(payload, dict):
            continue
        body = payload.get("content") or ""
        if payload.get("base64Encoded"):
            try:
                data = base64.b64decode(body)
            except Exception as exc:
                LOGGER.debug("Failed to decode base64 resource %s: %s", res_url, exc)
                continue
        else:
            data = str(body).encode("utf-8", errors="ignore")
        if _looks_like_pdf_bytes(data):
            return data, "resource"
    return None, "resource"


def _selenium_cdp_try_js_fetch_pdf(driver) -> tuple[bytes | None, str]:
    js_fetch = (
        "(async function() {"
        "try {"
        "  const resp = await fetch(window.location.href, {credentials: 'include'});"
        "  if (!resp || !resp.ok) { return null; }"
        "  const blob = await resp.blob();"
        f"  if (blob && blob.size && blob.size > {MAX_PDF}) {{ return null; }}"
        "  const b64 = await new Promise((resolve) => {"
        "    const reader = new FileReader();"
        "    reader.onloadend = () => {"
        "      const res = reader.result || '';"
        "      const idx = res.indexOf(',');"
        "      resolve(idx >= 0 ? res.slice(idx + 1) : null);"
        "    };"
        "    reader.onerror = () => resolve(null);"
        "    reader.readAsDataURL(blob);"
        "  });"
        "  return b64 || null;"
        "} catch (e) { return null; }"
        "})();"
    )
    try:
        result = driver.execute_cdp_cmd(
            "Runtime.evaluate",
            {"expression": js_fetch, "awaitPromise": True, "returnByValue": True},
        )
    except Exception as exc:
        LOGGER.debug("CDP js_fetch Runtime.evaluate failed: %s", exc)
        return None, "js_fetch"
    if not result or not isinstance(result, dict):
        return None, "js_fetch"
    payload = result.get("result") or {}
    b64_val = payload.get("value")
    if not b64_val:
        return None, "js_fetch"
    try:
        data = base64.b64decode(b64_val)
    except Exception as exc:
        LOGGER.debug("Failed to decode base64 from js_fetch: %s", exc)
        return None, "js_fetch"
    return (data, "js_fetch") if _looks_like_pdf_bytes(data) else (None, "js_fetch")


def _selenium_cdp_try_print_to_pdf(driver) -> tuple[bytes | None, str]:
    try:
        print_res = driver.execute_cdp_cmd(
            "Page.printToPDF",
            {"printBackground": True, "preferCSSPageSize": True},
        )
    except Exception as exc:
        LOGGER.debug("CDP printToPDF failed: %s", exc)
        return None, "printToPDF"
    if not print_res or not isinstance(print_res, dict):
        return None, "printToPDF"
    b64_pdf = print_res.get("data")
    if not b64_pdf:
        return None, "printToPDF"
    try:
        data = base64.b64decode(b64_pdf)
    except Exception as exc:
        LOGGER.debug("Failed to decode base64 from printToPDF: %s", exc)
        return None, "printToPDF"
    return (data, "printToPDF") if _looks_like_pdf_bytes(data) else (None, "printToPDF")

def bind_requests_session(_session) -> None:
    """
    Legacy compatibility hook.

    Selenium-only runtime does not share a requests session.
    """
    return None


# ------------------------------ WAF / Bot Detection ------------------------------


def _looks_like_cloudflare(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in CF_MARKERS)


def _wait_for_cloudflare_clear(page, *, max_total_seconds: float | None = None) -> None:
    """Best-effort wait for Cloudflare challenge interstitials to clear."""
    import time

    deadline = None
    if max_total_seconds is not None and max_total_seconds > 0:
        deadline = time.monotonic() + max_total_seconds

    for sel in CF_SELECTORS:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            timeout_ms = int(remaining * 1000)
        else:
            timeout_ms = 15_000

        try:
            page.wait_for_selector(sel, state="detached", timeout=timeout_ms)
        except Exception as exc:
            LOGGER.debug("Cloudflare wait selector failed (%s): %s", sel, exc)


# ------------------------------ ResearchGate / DOI / Unpaywall ------------------------------


def _looks_like_researchgate_block(html: str) -> bool:
    t = html.lower()
    return any(m in t for m in RG_BLOCK_MARKERS)


def _looks_like_bot_check_page(html: str, title: str | None = None) -> bool:
    """
    Best-effort detection of CAPTCHA / WAF interstitial pages.

    This is used to prevent returning `printToPDF` output for bot-check pages,
    which would otherwise save an interstitial as if it were a paper.
    """
    low_html = (html or "").lower()
    low_title = (title or "").lower()
    markers = (
        *CF_MARKERS,
        *RG_BLOCK_MARKERS,
        "captcha",
        "turnstile",
        "g-recaptcha",
        "hcaptcha",
        "challenge-form",
        "cf-challenge",
        "cf-turnstile",
    )
    return any(m in low_html or m in low_title for m in markers)


def _is_researchgate_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host == "www.researchgate.net" or host.endswith(".researchgate.net")
    except Exception as exc:
        LOGGER.debug("Failed to parse URL for researchgate check (%r): %s", url, exc)
        return False


def _researchgate_landing_url(url: str) -> str:
    """Normalize ResearchGate links to the public landing page."""
    try:
        p = urlparse(url)
        path = p.path
        for token in ("/links/", "/fulltext/"):
            if token in path:
                path = path.split(token, 1)[0]
                break
        return f"{p.scheme}://{p.netloc}{path}"
    except Exception as exc:
        LOGGER.debug("Failed to normalize ResearchGate URL %r: %s", url, exc)
        return url


_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)


def _extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    match = _DOI_RE.search(text)
    return match.group(0) if match else None


def _extract_doi_from_html(html: str) -> str | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        LOGGER.debug("Failed to parse HTML for DOI extraction: %s", exc)
        return None

    meta_keys = ("citation_doi", "dc.identifier", "doi")
    for key in meta_keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag:
            doi = _extract_doi(tag.get("content", ""))
            if doi:
                return doi

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            if NODRIVER_PDF_DEBUG:
                LOGGER.debug("Failed to parse ld+json while extracting DOI: %s", exc)
            continue
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                ident = item.get("identifier")
                if isinstance(ident, dict):
                    if str(ident.get("propertyID", "")).lower() == "doi":
                        doi = _extract_doi(ident.get("value"))
                        if doi:
                            return doi
                elif isinstance(ident, str):
                    doi = _extract_doi(ident)
                    if doi:
                        return doi
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return None


def _extract_text_from_html(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        LOGGER.debug("Failed to parse HTML for text extraction: %s", exc)
        return html
    pre = soup.find("pre")
    if pre:
        text = pre.get_text(strip=True)
        if text:
            return text
    text = soup.get_text(" ", strip=True)
    return text or html


def _selenium_fetch_text(url: str, *, referer: str | None = None) -> str:
    html = _render_with_selenium(url, referer=referer)
    return _extract_text_from_html(html)


def _selenium_fetch_json(url: str, *, referer: str | None = None) -> dict | None:
    text = _selenium_fetch_text(url, referer=referer).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        LOGGER.debug("Failed to parse JSON from %s (direct): %s", url, exc)
    m = re.search(r"({.*}|\\[.*\\])", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        LOGGER.debug("Failed to parse JSON from %s (regex fallback): %s", url, exc)
        return None


def _query_unpaywall(doi: str) -> str | None:
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        LOGGER.warning("[Unpaywall] UNPAYWALL_EMAIL not set; skipping.")
        return None
    api_url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    try:
        data = _selenium_fetch_json(api_url)
    except Exception as exc:
        LOGGER.warning("[Unpaywall] Request failed for %s: %s", doi, exc)
        return None
    if not data:
        LOGGER.warning("[Unpaywall] Empty response for %s", doi)
        return None

    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf")
    if pdf_url:
        return pdf_url
    for loc in data.get("oa_locations") or []:
        pdf_url = loc.get("url_for_pdf")
        if pdf_url:
            return pdf_url
    return None


# ------------------------------ bioRxiv / medRxiv S3 Fast Path ------------------------------


def _biorxiv_server_from_url(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
        if "medrxiv.org" in host:
            return "medrxiv"
        if "biorxiv.org" in host:
            return "biorxiv"
    except Exception as exc:
        LOGGER.debug("Failed to parse bio/medrxiv host from %r: %s", url, exc)
        return None
    return None


def _extract_biorxiv_doi(url: str) -> str | None:
    try:
        url = url.rstrip("/")
        path = urlparse(url).path
        if "/content/" in path:
            tail = path.split("/content/", 1)[1]
            tail = tail.strip("/")
            for suffix in (".full.pdf", ".full", ".pdf"):
                if tail.endswith(suffix):
                    tail = tail[: -len(suffix)]
                    break
            parts = tail.split("/")
            if parts:
                parts[-1] = re.sub(r"v\d+$", "", parts[-1])
            tail = "/".join(p for p in parts if p)
            if tail.startswith("10."):
                return tail
            if tail.startswith("biorxiv/early/") and parts:
                last = parts[-1]
                if re.match(r"\d{4}\.\d{2}\.\d{2}\.\d+", last):
                    return f"10.1101/{last}"
            if re.match(r"\d{4}\.\d{2}\.\d{2}\.\d+", tail):
                return f"10.1101/{tail}"
        m = re.search(r"(10\.\d{4,9}/[A-Za-z0-9._-]+)", url)
        if m:
            doi = re.sub(r"v\d+$", "", m.group(1))
            return doi
    except Exception as exc:
        LOGGER.debug("Failed to extract bio/medrxiv DOI from %r: %s", url, exc)
        return None
    return None


def _biorxiv_s3_prefix_from_details(payload: dict) -> str | None:
    try:
        collection = payload.get("collection") or []
        if not collection:
            return None
        latest = collection[-1]
        for key in ("jats_xml_path", "jatsxml_path", "jatsxml", "jats_xml"):
            if latest.get(key):
                val = latest[key]
                if val.startswith("http"):
                    # Use date to derive the monthly S3 prefix.
                    date_str = latest.get("date")
                    if date_str:
                        from datetime import datetime

                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        month = dt.strftime("%B")
                        return f"Current_Content/{month}_{dt.year}/"
                return os.path.dirname(val)
        date_str = latest.get("date")
        if date_str:
            from datetime import datetime

            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month = dt.strftime("%B")
            return f"Current_Content/{month}_{dt.year}/"
    except Exception as exc:
        LOGGER.debug("Failed to derive bio/medrxiv S3 prefix from API payload: %s", exc)
        return None
    return None


def _biorxiv_pick_pdf_key(keys: list[str], doi: str | None) -> str | None:
    pdfs = [k for k in keys if k.lower().endswith(".pdf")]
    if not pdfs:
        return None

    doi_suffix = doi.split("/")[-1] if doi else ""

    def score(k: str) -> tuple[int, int]:
        s = 0
        if "/content/" in k:
            s += 2
        if doi_suffix and doi_suffix in k:
            s += 1
        if "supp" in k.lower():
            s -= 1
        return (-s, len(k))

    return sorted(pdfs, key=score)[0]


def _boto3_client(access_key: str, secret_key: str):
    import boto3

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _biorxiv_iter_meca_keys(s3, bucket: str, prefix: str) -> list[str]:
    if prefix in S3_MONTH_KEYS_CACHE:
        return S3_MONTH_KEYS_CACHE[prefix]
    keys: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "RequestPayer": "requester"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        keys.extend([obj["Key"] for obj in resp.get("Contents", [])])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    meca_keys = [k for k in keys if k.lower().endswith(".meca")]
    S3_MONTH_KEYS_CACHE[prefix] = meca_keys
    return meca_keys


def _extract_pdf_from_meca_bytes(data: bytes, candidates: list[str]) -> bytes | None:
    import zipfile
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            for cand in candidates:
                if cand in names:
                    pdf_bytes = zf.read(cand)
                    if _looks_like_pdf_bytes(pdf_bytes):
                        return pdf_bytes
            # Fallback: first PDF in content/
            for name in names:
                if name.lower().endswith(".pdf") and name.startswith("content/"):
                    pdf_bytes = zf.read(name)
                    if _looks_like_pdf_bytes(pdf_bytes):
                        return pdf_bytes
    except Exception as exc:
        LOGGER.debug("Failed to extract PDF from MECA bytes: %s", exc)
        return None
    return None


def _fetch_biorxiv_s3_pdf(url: str) -> bytes | None:
    server = _biorxiv_server_from_url(url)
    if not server:
        return None

    access_key = os.environ.get("BIORXIV_S3_ACCESS_KEY")
    secret_key = os.environ.get("BIORXIV_S3_SECRET_KEY")
    if not access_key or not secret_key:
        return None

    doi = _extract_biorxiv_doi(url)
    if not doi:
        LOGGER.warning("[S3] Could not extract DOI from %s", url)
        return None

    def _biorxiv_date_range(d: str) -> tuple[str, str]:
        m = re.search(r"(\\d{4})\\.(\\d{2})\\.(\\d{2})", d)
        if m:
            y, mo, day = m.groups()
            date = f"{y}-{mo}-{day}"
            return date, date
        return "1900-01-01", "2100-01-01"

    start_date, end_date = _biorxiv_date_range(doi)
    api_url = f"https://api.biorxiv.org/details/{server}/{start_date}/{end_date}/{doi}"
    try:
        payload = _selenium_fetch_json(api_url)
    except Exception as exc:
        LOGGER.warning("[S3] BioRxiv API lookup failed for %s: %s", doi, exc)
        return None
    if not payload:
        LOGGER.warning("[S3] Empty API payload for %s", doi)
        return None

    prefix = _biorxiv_s3_prefix_from_details(payload)
    if not prefix:
        LOGGER.warning("[S3] No S3 prefix from API for %s", doi)
        return None

    bucket = BIORXIV_S3_BUCKET if server == "biorxiv" else MEDRXIV_S3_BUCKET
    try:
        s3 = _boto3_client(access_key, secret_key)
        # Direct PDF lookup if prefix includes a content directory
        if "/content/" in prefix:
            keys: list[str] = []
            token = None
            while True:
                kwargs = {"Bucket": bucket, "Prefix": prefix, "RequestPayer": "requester"}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kwargs)
                keys.extend([obj["Key"] for obj in resp.get("Contents", [])])
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
            pdf_key = _biorxiv_pick_pdf_key(keys, doi)
            if pdf_key:
                obj = s3.get_object(Bucket=bucket, Key=pdf_key, RequestPayer="requester")
                body = obj["Body"].read()
                if _looks_like_pdf_bytes(body):
                    LOGGER.info("[S3] Downloaded %s/%s", bucket, pdf_key)
                    return body

        # MECA fallback: scan monthly keys to find DOI PDF inside archive
        doi_suffix = doi.split("/")[-1]
        doi_tail = doi_suffix.split(".")[-1] if doi_suffix else ""
        candidates = []
        if doi_tail:
            candidates.append(f"content/{doi_tail}.pdf")
        if doi_suffix:
            candidates.append(f"content/{doi_suffix}.pdf")

        if doi_tail and doi_tail in S3_MECA_CACHE:
            meca_key = S3_MECA_CACHE[doi_tail]
            obj = s3.get_object(Bucket=bucket, Key=meca_key, RequestPayer="requester")
            pdf_bytes = _extract_pdf_from_meca_bytes(obj["Body"].read(), candidates)
            if pdf_bytes:
                LOGGER.info("[S3] Downloaded %s/%s -> %s.pdf", bucket, meca_key, doi_tail)
                return pdf_bytes

        meca_keys = _biorxiv_iter_meca_keys(s3, bucket, prefix)
        for meca_key in meca_keys:
            obj = s3.get_object(Bucket=bucket, Key=meca_key, RequestPayer="requester")
            pdf_bytes = _extract_pdf_from_meca_bytes(obj["Body"].read(), candidates)
            if pdf_bytes:
                if doi_tail:
                    S3_MECA_CACHE[doi_tail] = meca_key
                LOGGER.info("[S3] Downloaded %s/%s -> %s.pdf", bucket, meca_key, doi_tail or doi_suffix)
                return pdf_bytes

        LOGGER.info("[S3] No PDF found under %s/%s", bucket, prefix)
        return None
    except Exception as exc:
        LOGGER.warning("[S3] Failed to download PDF for %s: %s", doi, exc)
        return None


class FetchBlocked(RuntimeError):
    """Raised when a server blocks access to a resource."""
    pass


# ------------------------------ PDF Parsing (PyMuPDF4LLM) ------------------------------
def _parse_pdf_with_pymupdf4llm(pdf_bytes: bytes, *, source_url: str) -> List[Document]:
    try:
        import pymupdf4llm
    except Exception as e:
        raise RuntimeError(
            "pymupdf4llm is required to parse PDFs. Install with: pip install -U pymupdf4llm"
        ) from e

    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "doc.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        md_text = pymupdf4llm.to_markdown(pdf_path)
        if not isinstance(md_text, str) or not md_text.strip():
            raise RuntimeError("PyMuPDF4LLM produced empty output")

        return [Document(page_content=md_text, metadata={"source": source_url, "parser": "pymupdf4llm"})]


def _looks_like_pdf_bytes(data: bytes) -> bool:
    head = data[:1024]
    return b"%PDF-" in head


def _origin_from_url(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    return origin


def _landing_page_for_pdf(url: str) -> str | None:
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        path = p.path
        # biorxiv: .../content/<id>.full.pdf  -> .../content/<id>.full
        if p.netloc.endswith("biorxiv.org") and path.endswith(".full.pdf"):
            # normalize early path to DOI-based landing if possible
            # /content/biorxiv/early/YYYY/MM/DD/<doi_suffix>.full.pdf -> /content/10.1101/<doi_suffix>.full
            parts = path.split("/")
            if len(parts) >= 7 and parts[2] == "biorxiv" and parts[3] == "early":
                doi_suffix = parts[-1].replace(".full.pdf", "")
                return urlunsplit((p.scheme, p.netloc, f"/content/10.1101/{doi_suffix}.full", p.query, p.fragment))
            return urlunsplit((p.scheme, p.netloc, path[:-4], p.query, p.fragment))
        # cell.com: /<journal>/pdf/<id>.pdf -> /<journal>/fulltext/<id>
        if p.netloc.endswith("cell.com") and "/pdf/" in path and path.endswith(".pdf"):
            return urlunsplit((p.scheme, p.netloc, path.replace("/pdf/", "/fulltext/")[:-4], p.query, p.fragment))
        return None
    except Exception as exc:
        LOGGER.debug("Failed to infer landing page for PDF URL %r: %s", url, exc)
        return None


def _biorxiv_alt_pdf_urls(url: str) -> list[tuple[str, str]]:
    """Return alternative (pdf_url, referer) pairs for bioRxiv early URLs.

    For early paths, try DOI-based endpoints with and without version suffix.
    """
    out: list[tuple[str, str]] = []
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        if not p.netloc.endswith("biorxiv.org"):
            return out
        path = p.path
        if path.endswith(".full.pdf"):
            # DOI-based form: /content/10.1101/<suffix>v1.full.pdf -> versionless
            last = path.rsplit("/", 1)[-1]
            base = last.replace(".full.pdf", "")
            m = re.match(r"(.+?)v\d+$", base)
            if m:
                versionless = m.group(1)
                alt_path = path[: -len(last)] + f"{versionless}.full.pdf"
                alt_pdf = urlunsplit((p.scheme, p.netloc, alt_path, p.query, p.fragment))
                alt_ref = urlunsplit((p.scheme, p.netloc, alt_path[:-4], p.query, p.fragment))
                out.append((alt_pdf, alt_ref))

        if "/content/biorxiv/early/" in path and path.endswith(".full.pdf"):
            doi_suffix = path.split("/")[-1].replace(".full.pdf", "")
            base_landing = urlunsplit((p.scheme, p.netloc, f"/content/10.1101/{doi_suffix}.full", "", ""))
            out.append((base_landing + ".pdf", base_landing))
            out.append((base_landing.replace(".full", "v1.full") + ".pdf", base_landing.replace(".full", "v1.full")))
        return out
    except Exception as exc:
        LOGGER.debug("Failed to compute bioRxiv alt PDF URLs for %r: %s", url, exc)
        return out


def _with_download_query(url: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        p = urlsplit(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if q.get("download") == "1":
            return url
        q["download"] = "1"
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    except Exception as exc:
        LOGGER.debug("Failed to add download=1 query param to %r: %s", url, exc)
        if "?" in url:
            return url + "&download=1"
        return url + "?download=1"


def _load_pdf(url: str, referer: str | None = None) -> List[Document]:
    """
    Load a PDF URL and return parsed text as `Document` objects.

    This is a higher-level wrapper around `_fetch_pdf_via_selenium()` that also:
    - tries a bioRxiv/medRxiv S3 cache fast path (MECA/PDF buckets),
    - tries a few bioRxiv URL variants that often work better than the "canonical" URL,
    - parses the resulting PDF bytes via PyMuPDF4LLM into Markdown-like text.
    """
    landing = _landing_page_for_pdf(url)
    referer = referer or landing or None

    # S3 fast path for bioRxiv/medRxiv
    s3_bytes = _fetch_biorxiv_s3_pdf(url)
    if s3_bytes:
        return _parse_pdf_with_pymupdf4llm(s3_bytes, source_url=url)

    # bioRxiv special-case: try DOI-based alternatives before the original URL
    seen_alts: set[str] = set()
    for alt_pdf, alt_ref in _biorxiv_alt_pdf_urls(url):
        for cand in (alt_pdf, _with_download_query(alt_pdf)):
            if cand in seen_alts:
                continue
            seen_alts.add(cand)
            try:
                pdf_bytes_alt = _fetch_pdf_via_selenium(cand, alt_ref)
                return _parse_pdf_with_pymupdf4llm(pdf_bytes_alt, source_url=cand)
            except Exception as exc:
                LOGGER.debug("bioRxiv alt PDF fetch failed (%s): %s", cand, exc)
                continue

    try:
        pdf_bytes = _fetch_pdf_via_selenium(url, referer or _origin_from_url(url) + "/")
        return _parse_pdf_with_pymupdf4llm(pdf_bytes, source_url=url)
    except FetchBlocked:
        dl_url = _with_download_query(url)
        if dl_url != url:
            pdf_bytes = _fetch_pdf_via_selenium(dl_url, referer or _origin_from_url(dl_url) + "/")
            return _parse_pdf_with_pymupdf4llm(pdf_bytes, source_url=dl_url)
        raise

def _render_with_playwright(url: str, *, referer: str | None = None) -> str:
    """Render HTML with Selenium (Playwright alias)."""
    return _render_with_selenium(url, referer=referer)

def _likely_download_on_nav(u: str) -> bool:
    from urllib.parse import urlsplit
    h = urlsplit(u).netloc
    p = urlsplit(u).path.lower()
    return (
        "mdpi.com" in h
        or p.endswith(".pdf") and ("download" in p or "article-pdf" in p or "/pdf" in p)
    )


def _render_with_selenium(url: str, *, referer: str | None = None) -> str:
    """
    Render HTML using browser automation.

    Strategy:
    - Prefer **Nodriver** (Chromium) for a quick HTML snapshot because it often bypasses
      basic bot checks and executes page JS.
    - Fall back to **Selenium** (Chrome or Firefox) when Nodriver fails, returns a very
      small page, or appears to be a Cloudflare/WAF interstitial.
    """
    import time

    browser = (SELENIUM_BROWSER or "chrome").lower()
    if browser not in ("chrome", "firefox"):
        browser = "chrome"

    if browser == "firefox":
        from selenium import webdriver

        def _try_accept_consent(drv) -> None:
            # Best-effort: handle common cookie-consent banners quickly.
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC

                wait = WebDriverWait(drv, 2)
                xpath = (
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'accept') "
                    "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'agree') "
                    "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'i accept') "
                    "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'accept all') "
                    "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'i agree')]"
                )
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                btn.click()
            except Exception as exc:
                LOGGER.debug("Consent click attempt failed: %s", exc)
                return

        from selenium.webdriver.firefox.options import Options as FirefoxOptions

        opts = FirefoxOptions()
        if _selenium_headless():
            opts.add_argument("-headless")
        if SELENIUM_FIREFOX_BINARY:
            opts.binary_location = SELENIUM_FIREFOX_BINARY
        _apply_firefox_tor_prefs(opts)

        # Prefer an existing user profile when provided (Cloudflare clearance, logins, etc.).
        if SELENIUM_FIREFOX_PROFILE_PATH:
            opts.add_argument("-profile")
            opts.add_argument(SELENIUM_FIREFOX_PROFILE_PATH)

        # Best-effort anti-automation prefs (may be ignored by modern Firefox).
        try:
            opts.set_preference("dom.webdriver.enabled", False)
            opts.set_preference("useAutomationExtension", False)
        except Exception as exc:
            LOGGER.debug("Failed to set Firefox anti-automation prefs: %s", exc)

        driver = webdriver.Firefox(options=opts)
        try:
            driver.set_page_load_timeout(int(SELENIUM_PAGELOAD_TIMEOUT_SECONDS))
            if referer:
                try:
                    driver.get(referer)
                    time.sleep(0.5)
                except Exception as exc:
                    LOGGER.debug("Firefox referer navigation failed (%s): %s", referer, exc)
            driver.get(url)
            _try_accept_consent(driver)
            time.sleep(1.0)
            return driver.page_source or ""
        finally:
            try:
                driver.quit()
            except Exception as exc:
                LOGGER.debug("Failed to quit Firefox driver: %s", exc)

    def _render_with_chrome_selenium() -> str:
        from selenium.webdriver.chrome.options import Options as ChromeOptions

        opts = ChromeOptions()
        if not SELENIUM_DEBUGGER_ADDRESS and _selenium_headless():
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--user-agent={UA}")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        for arg in _chrome_tor_args():
            opts.add_argument(arg)

        if SELENIUM_DEBUGGER_ADDRESS:
            opts.add_experimental_option("debuggerAddress", SELENIUM_DEBUGGER_ADDRESS)

        driver = _create_chrome_driver(opts)
        _apply_canvas_spoof_cdp(driver)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
            )
        except Exception as exc:
            LOGGER.debug("Failed to apply navigator.webdriver patch via CDP: %s", exc)
        _apply_selenium_stealth(driver)
        try:
            driver.set_page_load_timeout(int(SELENIUM_PAGELOAD_TIMEOUT_SECONDS))
            if referer:
                try:
                    driver.get(referer)
                    time.sleep(0.5)
                except Exception as exc:
                    LOGGER.debug("Chrome referer navigation failed (%s): %s", referer, exc)
            driver.get(url)
            time.sleep(1.0)
            return driver.page_source or ""
        finally:
            try:
                driver.quit()
            except Exception as exc:
                LOGGER.debug("Failed to quit Chrome driver: %s", exc)

    if SELENIUM_DEBUGGER_ADDRESS:
        return _render_with_chrome_selenium()

    try:
        html = _nodriver_fetch_html(url, referer=referer)
        if html and len(html.strip()) > 200 and not _looks_like_cloudflare(html):
            return html
    except Exception as exc:
        LOGGER.debug("Nodriver HTML fetch failed for %s: %s", url, exc)
    return _render_with_chrome_selenium()

def _is_partial_download(path: "Path", partial_globs: tuple[str, ...]) -> bool:
    return any(path.match(glob) for glob in partial_globs)


def _pick_latest_download(download_dir: "Path", partial_globs: tuple[str, ...]) -> "Path | None":
    files = [p for p in download_dir.iterdir() if p.is_file()]
    candidates = [p for p in files if not _is_partial_download(p, partial_globs)]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _fetch_pdf_via_selenium_with_method(
    url: str,
    referer: str | None,
    *,
    sessions: PersistentBrowserSessions | None = None,
) -> tuple[bytes, str]:
    """
    Download a PDF using browser automation (Nodriver-first; Selenium fallback).

    The overall goal here is to retrieve the *actual PDF bytes* while minimizing:
    - WAF/bot-check blocks (Cloudflare, CAPTCHA pages)
    - repeated browser startups
    - long waits when the site never initiated a download

    High-level flow:
    1) Try Nodriver to fetch bytes directly (download/resource/js_fetch/printToPDF/page_content).
       - If Nodriver returns `printToPDF`, we treat it as a **fallback** (it may be a PDF of the
         rendered page, not the original PDF) and still attempt Selenium.
       - If Nodriver fails but captured cookies, those cookies are injected into Selenium.
    2) Use Selenium to attempt a real download into a temporary directory.
       - Chrome: prefer download-to-file, then CDP resource capture, then CDP `printToPDF`.
       - Firefox: use preferences to auto-save PDFs to disk (best-effort).
    3) If Selenium fails but Nodriver produced `printToPDF`, return that as a last resort.

    NOTE: This function name is kept for backwards compatibility with older
    scripts; new call sites should prefer `_fetch_pdf_via_browser_with_method()`.
    """
    import time
    from pathlib import Path

    url = (url or "").strip()
    low_url = url.lower()
    if not url or low_url in ("undefined", "null", "none", "about:blank"):
        raise ValueError(f"Invalid PDF URL: {url!r}")
    if not low_url.startswith(("http://", "https://")):
        raise ValueError(f"Unsupported URL scheme for PDF fetch: {url!r}")

    def _looks_blocked(html: str) -> bool:
        low = (html or "").lower()
        if "error reference number: 1020" in low:
            return True
        return _looks_like_cloudflare(low) or _looks_like_researchgate_block(low)

    # Fast-path: if Nodriver can fetch the PDF bytes directly (download/resource/js_fetch),
    # prefer that over spinning up a separate Selenium session.
    #
    # IMPORTANT: Cookies are captured within the SAME Nodriver session (no second Nodriver launch).
    nodriver_print_fallback: tuple[bytes, str] | None = None
    nodriver_cookies: list[dict] = []
    try:
        if sessions is not None:
            data, method, cookies = sessions.nodriver_fetch_pdf_bytes_and_cookies(url, referer=referer)
        else:
            data, method, cookies = _nodriver_fetch_pdf_bytes_and_cookies(url, referer=referer)
        if method != "printToPDF":
            return data, method
        nodriver_print_fallback = (data, method)
        nodriver_cookies = cookies
    except NodriverPdfFetchError as exc:
        nodriver_cookies = exc.cookies
        _pdf_debug(
            f"[Nodriver][PDF] Direct fetch failed: {exc} "
            f"(captured cookies={len(nodriver_cookies)}; will try Selenium)"
        )
    except Exception as exc:
        _pdf_debug(f"[Nodriver][PDF] Direct fetch failed: {exc} (will try Selenium)")
        nodriver_cookies = []

    with tempfile.TemporaryDirectory(
        prefix="selenium-pdf-",
        ignore_cleanup_errors=os.name == "nt",
    ) as d:
        download_dir = Path(d)

        browser = (SELENIUM_BROWSER or "chrome").lower()
        if browser not in ("chrome", "firefox"):
            browser = "chrome"

        if browser == "firefox":
            from selenium import webdriver
            from selenium.webdriver.firefox.options import Options as FirefoxOptions

            opts = FirefoxOptions()
            if _selenium_headless():
                opts.add_argument("-headless")
            if SELENIUM_FIREFOX_BINARY:
                opts.binary_location = SELENIUM_FIREFOX_BINARY
            _apply_firefox_tor_prefs(opts)

            profile = None
            profile_path = SELENIUM_FIREFOX_PROFILE_PATH
            if profile_path:
                try:
                    from selenium.webdriver.firefox.firefox_profile import FirefoxProfile

                    profile = FirefoxProfile(profile_path)
                except Exception as exc:
                    LOGGER.debug("Failed to load FirefoxProfile from %s: %s", profile_path, exc)
                    profile = None
            if profile is not None:
                _apply_firefox_tor_prefs(profile)

            # Download PDF to our temp dir without prompting.
            def _apply_download_prefs(target) -> None:
                try:
                    target.set_preference("browser.download.folderList", 2)
                    target.set_preference("browser.download.dir", str(download_dir))
                    target.set_preference("browser.download.useDownloadDir", True)
                    target.set_preference("browser.download.manager.showWhenStarting", False)
                    target.set_preference(
                        "browser.helperApps.neverAsk.saveToDisk",
                        "application/pdf,application/octet-stream",
                    )
                    target.set_preference("pdfjs.disabled", True)
                except Exception as exc:
                    LOGGER.debug("Failed to apply Firefox download prefs: %s", exc)

            _apply_download_prefs(opts)
            if profile is not None:
                _apply_download_prefs(profile)
                try:
                    opts.profile = profile
                except Exception as exc:
                    LOGGER.debug("Failed to attach FirefoxProfile to options: %s", exc)
            elif profile_path:
                opts.add_argument("-profile")
                opts.add_argument(profile_path)

            try:
                opts.set_preference("dom.webdriver.enabled", False)
                opts.set_preference("useAutomationExtension", False)
            except Exception as exc:
                LOGGER.debug("Failed to set Firefox anti-automation prefs: %s", exc)

            driver = webdriver.Firefox(options=opts)
            partial_globs = ("*.part", "*.partial", "*.tmp")
            try:
                driver.set_page_load_timeout(int(SELENIUM_PAGELOAD_TIMEOUT_SECONDS))
                _pdf_debug(f"[Selenium][PDF] Starting Firefox download (cookies={len(nodriver_cookies)}): {url}")
                _selenium_inject_cookies(driver, nodriver_cookies, url=url, referer=referer)
                if referer:
                    try:
                        driver.get(referer)
                        time.sleep(0.5)
                    except Exception as exc:
                        LOGGER.debug("Firefox referer navigation failed (%s): %s", referer, exc)
                driver.get(url)
                time.sleep(0.5)

                page_source = driver.page_source or ""
                page_title = ""
                try:
                    page_title = driver.title or ""
                except Exception as exc:
                    LOGGER.debug("Firefox driver.title failed: %s", exc)
                if _looks_blocked(page_source) or _looks_like_bot_check_page(page_source, page_title):
                    _pdf_debug(f"[Selenium][PDF] Detected WAF/CAPTCHA page after navigation: {url}")
                    raise FetchBlocked("WAF/CAPTCHA blocked")

                deadline = time.monotonic() + float(SELENIUM_DOWNLOAD_TIMEOUT_SECONDS)
                last_size = None
                last_file = None
                stable_count = 0
                while time.monotonic() < deadline:
                    p = _pick_latest_download(download_dir, partial_globs)
                    if p is not None:
                        if last_file is None or p != last_file:
                            last_file = p
                            last_size = None
                            stable_count = 0
                        size = p.stat().st_size
                        if size > 0:
                            if size == last_size:
                                stable_count += 1
                                if stable_count >= 3:
                                    for retry in range(3):
                                        try:
                                            data = p.read_bytes()
                                            break
                                        except PermissionError:
                                            time.sleep(0.1 * (2 ** retry))
                                    else:
                                        time.sleep(0.25)
                                        continue
                                    if not _looks_like_pdf_bytes(data):
                                        raise FetchBlocked("Non-PDF response")
                                    return data, "download"
                            else:
                                last_size = size
                                stable_count = 0
                    time.sleep(0.25)

                p = _pick_latest_download(download_dir, partial_globs)
                if p is not None:
                    try:
                        data = p.read_bytes()
                        if _looks_like_pdf_bytes(data):
                            return data, "download"
                    except PermissionError:
                        pass

                raise FetchBlocked(f"Selenium download timed out for {url}")
            finally:
                try:
                    driver.quit()
                except Exception as exc:
                    LOGGER.debug("Failed to quit Firefox driver (pdf fetch): %s", exc)

        def _chrome_download_with_selenium() -> tuple[bytes, str]:
            from selenium.webdriver.chrome.options import Options as ChromeOptions

            driver = None
            owns_driver = True
            if sessions is not None:
                try:
                    driver = sessions.acquire_chrome_selenium_driver()
                except Exception as exc:
                    if NODRIVER_PDF_DEBUG:
                        _pdf_debug(f"[Selenium][PDF] Persistent Chrome driver unavailable: {exc}")
                    driver = None
                if driver is not None:
                    owns_driver = False

            if driver is None:
                opts = ChromeOptions()
                if not SELENIUM_DEBUGGER_ADDRESS and _selenium_headless():
                    opts.add_argument("--headless=new")
                opts.add_argument("--disable-blink-features=AutomationControlled")
                opts.add_argument(f"--user-agent={UA}")
                opts.add_argument("--window-size=1920,1080")
                opts.add_argument("--disable-gpu")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                for arg in _chrome_tor_args():
                    opts.add_argument(arg)

                prefs = {
                    "download.default_directory": str(download_dir),
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "profile.default_content_settings.popups": 0,
                    "plugins.always_open_pdf_externally": True,
                }
                opts.add_experimental_option("prefs", prefs)

                if SELENIUM_DEBUGGER_ADDRESS:
                    opts.add_experimental_option("debuggerAddress", SELENIUM_DEBUGGER_ADDRESS)

                if NODRIVER_PDF_DEBUG:
                    _pdf_debug(
                        "[Selenium][PDF] Launching Chrome driver "
                        f"(undetected={SELENIUM_USE_UNDETECTED} headless={_selenium_headless()}): {url}"
                    )
                try:
                    driver = _create_chrome_driver(opts)
                except Exception as exc:
                    _pdf_debug(f"[Selenium][PDF] Chrome driver startup failed: {exc}")
                    _pdf_debug(f"[Selenium][PDF] Falling back to Nodriver PDF bytes: {url}")
                    if sessions is not None:
                        return sessions.nodriver_fetch_pdf_bytes(url, referer=referer)
                    return _nodriver_fetch_pdf_bytes(url, referer=referer)

                _apply_canvas_spoof_cdp(driver)
                try:
                    driver.execute_cdp_cmd(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
                    )
                except Exception as exc:
                    LOGGER.debug("Failed to apply navigator.webdriver patch via CDP: %s", exc)
                _apply_selenium_stealth(driver)
            partial_globs = ("*.crdownload",)
            try:
                driver.set_page_load_timeout(int(SELENIUM_PAGELOAD_TIMEOUT_SECONDS))
                _pdf_debug(f"[Selenium][PDF] Starting Chrome download (cookies={len(nodriver_cookies)}): {url}")
                _selenium_inject_cookies(driver, nodriver_cookies, url=url, referer=referer)

                persistent_download_root = None
                if sessions is not None and not owns_driver:
                    persistent_download_root = getattr(sessions, "_selenium_download_root", None)

                if referer:
                    try:
                        driver.get(referer)
                        time.sleep(0.5)
                    except Exception as exc:
                        LOGGER.debug("Chrome referer navigation failed (%s): %s", referer, exc)

                download_start_mtime = time.time()
                download_behavior_ok = True
                try:
                    driver.execute_cdp_cmd(
                        "Page.setDownloadBehavior",
                        {"behavior": "allow", "downloadPath": str(download_dir)},
                    )
                except Exception as exc:
                    download_behavior_ok = False
                    _pdf_debug(f"[Selenium][PDF] setDownloadBehavior failed: {exc}")
                    if _is_selenium_connection_refused(exc):
                        raise

                driver.get(url)
                time.sleep(0.5)

                if _looks_blocked(driver.page_source or ""):
                    raise FetchBlocked("Cloudflare blocked")

                def _pick_persistent_download() -> Path | None:
                    if not persistent_download_root:
                        return None
                    try:
                        root_dir = Path(persistent_download_root)
                        files = [p for p in root_dir.iterdir() if p.is_file()]
                    except Exception as exc:
                        LOGGER.debug("Failed to scan persistent download dir %s: %s", persistent_download_root, exc)
                        return None
                    candidates = [
                        p
                        for p in files
                        if p.stat().st_mtime >= download_start_mtime
                        and not _is_partial_download(p, partial_globs)
                    ]
                    if not candidates:
                        return None
                    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    return candidates[0]

                deadline = time.monotonic() + float(SELENIUM_DOWNLOAD_TIMEOUT_SECONDS)
                last_size = None
                last_file = None
                stable_count = 0
                while time.monotonic() < deadline:
                    p = _pick_latest_download(download_dir, partial_globs)
                    if p is None and persistent_download_root and not download_behavior_ok:
                        p = _pick_persistent_download()
                    if p is not None:
                        if last_file is None or p != last_file:
                            last_file = p
                            last_size = None
                            stable_count = 0
                        size = p.stat().st_size
                        if size > 0:
                            if size == last_size:
                                stable_count += 1
                                if stable_count >= 3:
                                    for retry in range(3):
                                        try:
                                            data = p.read_bytes()
                                            break
                                        except PermissionError:
                                            time.sleep(0.1 * (2 ** retry))
                                    else:
                                        time.sleep(0.25)
                                        continue
                                    if not _looks_like_pdf_bytes(data):
                                        raise FetchBlocked("Non-PDF response")
                                    _pdf_debug(f"[Selenium][PDF] Saved via download: {url}")
                                    return data, "download"
                            else:
                                last_size = size
                                stable_count = 0
                    time.sleep(0.25)

                p = _pick_latest_download(download_dir, partial_globs)
                if p is None and persistent_download_root and not download_behavior_ok:
                    p = _pick_persistent_download()
                if p is not None:
                    try:
                        data = p.read_bytes()
                        if _looks_like_pdf_bytes(data):
                            _pdf_debug(f"[Selenium][PDF] Saved via download(last_chance): {url}")
                            return data, "download"
                    except PermissionError:
                        pass

                data, method = _selenium_cdp_try_resource_pdf(driver, url)
                if data:
                    _pdf_debug(f"[Selenium][PDF] Saved via {method}: {url}")
                    return data, method
                data, method = _selenium_cdp_try_js_fetch_pdf(driver)
                if data:
                    _pdf_debug(f"[Selenium][PDF] Saved via {method}: {url}")
                    return data, method
                title_now = ""
                try:
                    title_now = driver.title or ""
                except Exception as exc:
                    LOGGER.debug("Chrome driver.title failed during bot-check detection: %s", exc)
                if _looks_like_bot_check_page(driver.page_source or "", title_now):
                    _pdf_debug(f"[Selenium][PDF] Detected WAF/CAPTCHA page; refusing printToPDF: {url}")
                    raise FetchBlocked("WAF/CAPTCHA interstitial (refusing printToPDF)")
                data, method = _selenium_cdp_try_print_to_pdf(driver)
                if data:
                    _pdf_debug(f"[Selenium][PDF] Saved via {method}: {url}")
                    return data, method

                raise FetchBlocked(f"Selenium download timed out for {url}")
            finally:
                if owns_driver:
                    try:
                        driver.quit()
                    except Exception as exc:
                        LOGGER.debug("Failed to quit Chrome driver (pdf fetch): %s", exc)

        def _restart_selenium_for_retry() -> None:
            if sessions is not None:
                sessions._stop_selenium_best_effort()

        try:
            return _retry_once_on_selenium_connection_refused(
                _chrome_download_with_selenium,
                on_restart=_restart_selenium_for_retry,
                url=url,
                label="PDF",
            )
        except Exception as exc:
            _pdf_debug(f"[Selenium][PDF] Chrome Selenium flow failed: {exc}")
            if nodriver_print_fallback is not None:
                _pdf_debug(f"[Selenium][PDF] Returning Nodriver printToPDF fallback: {url}")
                return nodriver_print_fallback
            _pdf_debug(f"[Selenium][PDF] No Nodriver printToPDF fallback; failing candidate: {url}")
            # Nodriver was already attempted once for this URL; fail this candidate and let the caller
            # move to the next URL rather than re-running Nodriver again.
            raise FetchBlocked(f"Selenium Chrome flow failed for {url}: {exc}") from exc


def _fetch_pdf_via_browser_with_method(
    url: str,
    referer: str | None,
    *,
    sessions: PersistentBrowserSessions | None = None,
) -> tuple[bytes, str]:
    """Preferred alias for `_fetch_pdf_via_selenium_with_method()` (Nodriver-first)."""
    return _fetch_pdf_via_selenium_with_method(url, referer, sessions=sessions)


def _fetch_pdf_via_selenium(url: str, referer: str | None) -> bytes:
    """Compatibility wrapper: return only PDF bytes (no method string)."""
    data, _method = _fetch_pdf_via_selenium_with_method(url, referer)
    return data

def _fetch_pdf_via_playwright(url: str, referer: str | None) -> bytes:
    """Selenium-only runtime; keep name for compatibility."""
    return _fetch_pdf_via_selenium(url, referer)

def _fetch_researchgate_pdf_via_playwright(
    url: str,
    landing_url: str | None,
    *,
    budget_seconds: int = 75,
) -> bytes:
    """Selenium-only runtime; keep name for compatibility."""
    return _fetch_pdf_via_selenium(url, landing_url)

def _load_researchgate_pdf(url: str) -> list[Document]:
    """
    ResearchGate-specific loader.

    ResearchGate pages often require cookies/JS and can block direct downloads.
    We try the cheaper path first:
    - Derive a DOI from the URL or landing page.
    - Use Unpaywall (requires `UNPAYWALL_EMAIL`) to find an open-access PDF.
    - If Unpaywall fails, fall back to browser-driven PDF fetch.
    """
    landing_url = _researchgate_landing_url(url)
    doi = _extract_doi(url) or _extract_doi(landing_url)

    if not doi:
        LOGGER.info("[ResearchGate] Probing landing page for DOI: %s", landing_url)
        html = _render_with_selenium(landing_url)
        doi = _extract_doi_from_html(html)

    if doi:
        pdf_url = _query_unpaywall(doi)
        if pdf_url:
            LOGGER.info("[ResearchGate] Unpaywall PDF for %s: %s", doi, pdf_url)
            try:
                return _load_pdf(pdf_url, referer=landing_url)
            except Exception as exc:
                LOGGER.warning("[ResearchGate] Unpaywall PDF failed: %s", exc)

    LOGGER.info("[ResearchGate] Falling back to selenium for %s", url)
    pdf_bytes = _fetch_pdf_via_selenium(url, landing_url)
    return _parse_pdf_with_pymupdf4llm(pdf_bytes, source_url=url)

def _load_html(url: str) -> list[Document]:
    """Render HTML in a browser and extract plain text via BeautifulSoup."""
    html = _render_with_selenium(url)
    text = _extract_text_from_html(html)
    if _looks_like_cloudflare(text):
        LOGGER.warning("[HTML] Cloudflare interstitial detected for %s", url)
    return [Document(page_content=text, metadata={"source": url})]

class TimeoutException(Exception):
    """Raised when operation times out."""
    pass


def _run_with_timeout(func, args, timeout_seconds=60):
    """Run a function with a timeout - simplified for process safety."""
    import platform
    import signal

    # On Windows, we can't use signal-based timeouts in a process pool
    if platform.system() == 'Windows':
        # Use thread-based timeout to avoid hanging on Windows
        executor = _get_windows_executor()
        future = executor.submit(func, *args)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            # Best-effort cancel; underlying work may still run to completion.
            try:
                future.cancel()
            except Exception as exc:
                LOGGER.debug("Failed to cancel timed-out future: %s", exc)
            raise TimeoutException(f"Timed out after {timeout_seconds} seconds")

    # On Unix-like systems, we can use signal
    def timeout_handler(signum, frame):
        raise TimeoutException(f"Timed out after {timeout_seconds} seconds")

    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout_seconds)

    try:
        result = func(*args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    return result


def _is_pdf_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return path.endswith(".pdf")
    except Exception as exc:
        LOGGER.debug("Failed to parse URL for pdf-url check (%r): %s", url, exc)
        return url.lower().endswith(".pdf")


# ------------------------------ Public API ------------------------------


def load_url(url: str, *, referer: str | None = None) -> List[Document]:
    """
    Universal loader entrypoint.

    - Routes ResearchGate URLs through `_load_researchgate_pdf()`.
    - Routes obvious PDF URLs through `_load_pdf()`.
    - Everything else is treated as HTML and rendered/extracted via `_load_html()`.

    This function wraps each operation in a coarse timeout and returns `[]` on timeout.
    """
    try:
        if _is_researchgate_url(url):
            return _run_with_timeout(_load_researchgate_pdf, (url,), timeout_seconds=180)
        if _is_pdf_url(url):
            return _run_with_timeout(_load_pdf, (url, referer), timeout_seconds=60)

        return _run_with_timeout(_load_html, (url,), timeout_seconds=60)
    except TimeoutException:
        LOGGER.warning("Timeout loading %s", url)
        return []
