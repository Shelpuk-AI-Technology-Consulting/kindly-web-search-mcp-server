from __future__ import annotations

import atexit
import asyncio
import contextlib
import os
import random
import socket
import tempfile
import time
from dataclasses import dataclass, field
from typing import Iterable

from ..utils.diagnostics import Diagnostics
from . import nodriver_worker as worker

DEFAULT_POOL_SIZE = 1
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 30.0


def _resolve_reuse_enabled() -> bool:
    raw = (os.environ.get("KINDLY_NODRIVER_REUSE_BROWSER") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _resolve_pool_size() -> int:
    raw = (os.environ.get("KINDLY_NODRIVER_BROWSER_POOL_SIZE") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_POOL_SIZE
    if value <= 0:
        value = DEFAULT_POOL_SIZE
    return max(1, min(value, 10))


def _resolve_acquire_timeout_seconds() -> float:
    raw = (os.environ.get("KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    if value <= 0:
        value = DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    return max(0.5, min(value, 300.0))


def _parse_port_range(raw: str) -> tuple[int, int] | None:
    if not raw:
        return None
    parts = raw.split("-", 1)
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0].strip())
        end = int(parts[1].strip())
    except ValueError:
        return None
    if start <= 0 or end <= 0 or end < start:
        return None
    return (start, end)


def _resolve_port_range() -> tuple[int, int] | None:
    raw = (os.environ.get("KINDLY_NODRIVER_PORT_RANGE") or "").strip()
    return _parse_port_range(raw)


def _iter_ports_in_range(start: int, end: int) -> Iterable[int]:
    ports = list(range(start, end + 1))
    random.shuffle(ports)
    return ports


def _pick_port_from_range(host: str, port_range: tuple[int, int]) -> int:
    start, end = port_range
    for port in _iter_ports_in_range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free ports available in range {start}-{end}")


def _pick_port(host: str, port_range: tuple[int, int] | None) -> int:
    if port_range is None:
        return worker._pick_free_port(host)
    return _pick_port_from_range(host, port_range)


def _resolve_browser_executable_path() -> str | None:
    return worker._resolve_browser_executable_path(None)


def _default_user_agent() -> str:
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _base_browser_args(user_agent: str, sandbox_enabled: bool) -> list[str]:
    return [
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


@dataclass
class ChromiumSlot:
    slot_id: int
    host: str = "127.0.0.1"
    port: int | None = None
    proc: asyncio.subprocess.Process | None = None
    user_data_dir: tempfile.TemporaryDirectory[str] | None = None
    browser_executable_path: str | None = None
    last_started: float | None = None

    async def ensure_started(
        self,
        *,
        user_agent: str,
        port_range: tuple[int, int] | None,
        diagnostics: Diagnostics | None,
    ) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return
        await self._start(user_agent=user_agent, port_range=port_range, diagnostics=diagnostics)

    async def _start(
        self,
        *,
        user_agent: str,
        port_range: tuple[int, int] | None,
        diagnostics: Diagnostics | None,
    ) -> None:
        self.browser_executable_path = _resolve_browser_executable_path()
        if not self.browser_executable_path:
            raise RuntimeError(
                "No Chromium-based browser executable found. "
                "Install Chromium/Chrome or set KINDLY_BROWSER_EXECUTABLE_PATH."
            )
        sandbox_enabled = worker._resolve_sandbox_enabled()
        devtools_ready_timeout_seconds = worker._resolve_devtools_ready_timeout_seconds()
        is_snap = worker._is_snap_browser(self.browser_executable_path)
        if is_snap:
            devtools_ready_timeout_seconds *= worker._resolve_snap_backoff_multiplier()

        if self.user_data_dir is None:
            self.user_data_dir = tempfile.TemporaryDirectory(
                prefix="kindly-nodriver-pool-", ignore_cleanup_errors=True
            )
        self.port = _pick_port(self.host, port_range)

        args = worker._build_chromium_launch_args(
            base_browser_args=_base_browser_args(user_agent, sandbox_enabled),
            user_data_dir=self.user_data_dir.name,
            user_agent=user_agent,
            host=self.host,
            port=self.port,
            sandbox_enabled=sandbox_enabled,
        )
        if diagnostics:
            diagnostics.emit(
                "pool.slot_start",
                "Starting pooled Chromium",
                {
                    "slot_id": self.slot_id,
                    "host": self.host,
                    "port": self.port,
                    "user_data_dir": self.user_data_dir.name,
                },
            )
        self.proc = await worker._launch_chromium(self.browser_executable_path, args)
        await worker._wait_for_devtools_ready(
            host=self.host,
            port=self.port,
            proc=self.proc,
            timeout_seconds=devtools_ready_timeout_seconds,
        )
        self.last_started = time.monotonic()
        if diagnostics:
            diagnostics.emit(
                "pool.slot_ready",
                "Pooled Chromium ready",
                {"slot_id": self.slot_id, "host": self.host, "port": self.port},
            )

    async def terminate(self) -> None:
        if self.proc is not None:
            await worker._terminate_process(self.proc)
            self.proc = None
        if self.user_data_dir is not None:
            self.user_data_dir.cleanup()
            self.user_data_dir = None

    def terminate_sync(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                time.sleep(0.2)
                if proc.returncode is None:
                    proc.kill()
        except Exception:
            pass
        self.proc = None
        if self.user_data_dir is not None:
            with contextlib.suppress(Exception):
                self.user_data_dir.cleanup()
            self.user_data_dir = None


@dataclass
class ChromiumPool:
    size: int
    acquire_timeout_seconds: float
    port_range: tuple[int, int] | None
    slots: list[ChromiumSlot] = field(default_factory=list)
    queue: asyncio.Queue[ChromiumSlot] = field(default_factory=asyncio.Queue)

    def __post_init__(self) -> None:
        for idx in range(self.size):
            slot = ChromiumSlot(slot_id=idx)
            self.slots.append(slot)
            self.queue.put_nowait(slot)

    async def acquire(
        self, *, user_agent: str, diagnostics: Diagnostics | None
    ) -> ChromiumSlot | None:
        try:
            slot = await asyncio.wait_for(
                self.queue.get(), timeout=self.acquire_timeout_seconds
            )
        except asyncio.TimeoutError:
            if diagnostics:
                diagnostics.emit(
                    "pool.acquire_timeout",
                    "Timed out waiting for pooled Chromium",
                    {"timeout_seconds": self.acquire_timeout_seconds},
                )
            return None

        try:
            await slot.ensure_started(
                user_agent=user_agent, port_range=self.port_range, diagnostics=diagnostics
            )
        except Exception as exc:
            if diagnostics:
                diagnostics.emit(
                    "pool.slot_error",
                    "Failed to start pooled Chromium",
                    {"slot_id": slot.slot_id, "error": type(exc).__name__},
                )
            with contextlib.suppress(Exception):
                await slot.terminate()
            await self.release(slot, diagnostics=diagnostics)
            return None

        if diagnostics:
            diagnostics.emit(
                "pool.acquire",
                "Acquired pooled Chromium slot",
                {"slot_id": slot.slot_id, "host": slot.host, "port": slot.port},
            )
        return slot

    async def release(self, slot: ChromiumSlot, *, diagnostics: Diagnostics | None) -> None:
        if diagnostics:
            diagnostics.emit(
                "pool.release",
                "Released pooled Chromium slot",
                {"slot_id": slot.slot_id, "host": slot.host, "port": slot.port},
            )
        await self.queue.put(slot)

    async def shutdown(self) -> None:
        for slot in self.slots:
            await slot.terminate()

    def shutdown_sync(self) -> None:
        for slot in self.slots:
            slot.terminate_sync()


_POOL: ChromiumPool | None = None
_POOL_LOCK = asyncio.Lock()
_SHUTDOWN_REGISTERED = False


async def get_chromium_pool(diagnostics: Diagnostics | None = None) -> ChromiumPool:
    global _POOL
    if _POOL is not None:
        return _POOL
    async with _POOL_LOCK:
        if _POOL is None:
            _POOL = ChromiumPool(
                size=_resolve_pool_size(),
                acquire_timeout_seconds=_resolve_acquire_timeout_seconds(),
                port_range=_resolve_port_range(),
            )
            if diagnostics:
                diagnostics.emit(
                    "pool.init",
                    "Initialized Chromium pool",
                    {"size": _POOL.size, "port_range": _POOL.port_range},
                )
            _register_shutdown(_POOL)
    return _POOL


def reuse_enabled() -> bool:
    return _resolve_reuse_enabled()


def _register_shutdown(pool: ChromiumPool) -> None:
    global _SHUTDOWN_REGISTERED
    if _SHUTDOWN_REGISTERED:
        return
    _SHUTDOWN_REGISTERED = True

    def _shutdown() -> None:
        pool.shutdown_sync()

    atexit.register(_shutdown)
