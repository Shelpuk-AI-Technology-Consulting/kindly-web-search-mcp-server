from __future__ import annotations

import atexit
import asyncio
import contextlib
import math
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
POOL_HEALTH_TIMEOUT_SECONDS = 2.0


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


def _resolve_idle_timeout_seconds() -> float | None:
    """Read how long a released pooled browser may sit unused before it is closed.

    Off unless configured, so a server that sets nothing never closes a browser
    for being idle. Any positive, finite number of seconds is honoured as given,
    with no clamp: a small value costs a cold start on most requests, which is
    the trade the operator asked for, and ``KINDLY_NODRIVER_REUSE_BROWSER=0``
    already covers anyone who wants a fresh browser on every request.

    Returns:
        The timeout in seconds, or ``None`` -- idle closing off -- when
        ``KINDLY_NODRIVER_BROWSER_IDLE_TIMEOUT_SECONDS`` is unset, empty, not a
        number, zero, negative, infinite or NaN.
    """
    raw = (os.environ.get("KINDLY_NODRIVER_BROWSER_IDLE_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


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
    return worker._resolve_user_agent(_resolve_browser_executable_path())


def _base_browser_args(user_agent: str, sandbox_enabled: bool) -> list[str]:
    return [
        "--window-size=1920,1080",
        *([] if sandbox_enabled else ["--no-sandbox"]),
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--disable-logging",
        "--log-level=3",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        f"--user-agent={user_agent}",
    ]


def _holds_running_browser(slot: ChromiumSlot) -> bool:
    """Say whether ``slot`` has a browser process that has not exited.

    Args:
        slot: The slot to inspect.

    Returns:
        ``True`` if ``slot.proc`` is set and has no return code.
    """
    return slot.proc is not None and slot.proc.returncode is None


@dataclass
class ChromiumSlot:
    """One pooled Chromium: the process, its DevTools endpoint and its profile.

    A slot outlives its browser. :meth:`ensure_started` launches a fresh one
    whenever ``proc`` is ``None`` -- before the slot's first use, after a
    terminate, after an idle close -- or has exited.

    Attributes:
        slot_id: The slot's index in :attr:`ChromiumPool.slots`, for diagnostics.
        host: The address the browser's DevTools endpoint listens on.
        port: That endpoint's port, or ``None`` before the first launch.
        proc: The browser process, or ``None`` while the slot holds none.
        user_data_dir: The browser's profile directory, removed on terminate.
        browser_executable_path: The executable the last launch resolved.
        last_started: :func:`time.monotonic` when the last launch became ready.
        idle_timer: The pending idle close, armed by :meth:`ChromiumPool.release`
            and disarmed by :meth:`ChromiumPool.acquire`, so it is ``None``
            whenever the slot is held.
        idle_closed: Set when an idle close took this slot's browser, and cleared
            by the next acquire, which reports it: that request is the one paying
            for the cold start.
    """

    slot_id: int
    host: str = "127.0.0.1"
    port: int | None = None
    proc: asyncio.subprocess.Process | None = None
    user_data_dir: tempfile.TemporaryDirectory[str] | None = None
    browser_executable_path: str | None = None
    last_started: float | None = None
    idle_timer: asyncio.TimerHandle | None = None
    idle_closed: bool = False

    async def ensure_started(
        self,
        *,
        user_agent: str,
        port_range: tuple[int, int] | None,
        diagnostics: Diagnostics | None,
    ) -> None:
        if self.proc is not None and self.proc.returncode is None:
            if self.port is None:
                if diagnostics:
                    diagnostics.emit(
                        "pool.slot_probe_failed",
                        "Pooled Chromium missing port",
                        {"slot_id": self.slot_id},
                    )
                await self.terminate()
            else:
                try:
                    await worker._wait_for_devtools_ready(
                        host=self.host,
                        port=self.port,
                        proc=self.proc,
                        timeout_seconds=POOL_HEALTH_TIMEOUT_SECONDS,
                    )
                    if diagnostics:
                        diagnostics.emit(
                            "pool.slot_probe",
                            "Pooled Chromium health check ok",
                            {"slot_id": self.slot_id, "port": self.port},
                        )
                    return
                except Exception as exc:
                    if diagnostics:
                        diagnostics.emit(
                            "pool.slot_probe_failed",
                            "Pooled Chromium health check failed",
                            {
                                "slot_id": self.slot_id,
                                "port": self.port,
                                "error": type(exc).__name__,
                            },
                        )
                    await self.terminate()
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
    """A fixed set of :class:`ChromiumSlot`, handed out one caller at a time.

    Attributes:
        size: How many slots the pool holds.
        acquire_timeout_seconds: How long :meth:`acquire` waits for a free slot.
        port_range: The inclusive range DevTools ports are picked from, or
            ``None`` for any free port.
        idle_timeout_seconds: How long a released slot's browser may sit unused
            before it is closed, or ``None`` to never close one for being idle.
        slots: Every slot, held or queued.
        queue: The slots free to acquire.
        retiring: Browsers taken off their slot and still being terminated,
            keyed by the task doing it. Held so that both shutdown paths reach
            them -- a detached browser is no longer in ``slots`` -- and so the
            event loop's weak reference is not the task's only one. A task that
            was cancelled stays: that is what the event loop's own shutdown does
            to it, and ``shutdown_sync`` at interpreter exit is then the only
            thing left to terminate its browser.
    """

    size: int
    acquire_timeout_seconds: float
    port_range: tuple[int, int] | None
    idle_timeout_seconds: float | None = None
    slots: list[ChromiumSlot] = field(default_factory=list)
    queue: asyncio.Queue[ChromiumSlot] = field(default_factory=asyncio.Queue)
    retiring: dict[asyncio.Task[None], ChromiumSlot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for idx in range(self.size):
            slot = ChromiumSlot(slot_id=idx)
            self.slots.append(slot)
            self.queue.put_nowait(slot)

    async def acquire(
        self, *, user_agent: str, diagnostics: Diagnostics | None
    ) -> ChromiumSlot | None:
        """Take a slot off the queue and make sure its browser is running.

        Args:
            user_agent: The user agent a newly launched browser is started with.
            diagnostics: Where to record the acquisition, or ``None``.

        Returns:
            A slot with a live browser, or ``None`` when none became free within
            ``acquire_timeout_seconds`` or its browser failed to start.

        Raises:
            asyncio.CancelledError: If the caller is cancelled while the slot's
                browser is being probed or started. The slot is back in the
                queue by then, without its browser.
        """
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

        # Once the slot is off the queue a request holds it: disarm before
        # awaiting anything else, so the timer cannot close a browser in use.
        self._disarm_idle_close(slot)
        if slot.idle_closed:
            slot.idle_closed = False
            if diagnostics:
                diagnostics.emit(
                    "pool.slot_idle_closed",
                    "Pooled Chromium was closed while idle; starting a fresh one",
                    {
                        "slot_id": slot.slot_id,
                        "idle_timeout_seconds": self.idle_timeout_seconds,
                    },
                )

        try:
            await slot.ensure_started(
                user_agent=user_agent, port_range=self.port_range, diagnostics=diagnostics
            )
        except BaseException as exc:
            # The caller never received the slot, so its own `finally` cannot
            # return it, and a slot lost here wedges a pool of one for the rest
            # of the process. A cancellation -- a caller's deadline landing
            # mid-probe or mid-launch -- may be what raised, and any await in
            # this handler would be one more place for one to land, so nothing
            # below awaits. Whatever the slot holds goes to the background
            # terminate, since nothing can tell whether an interrupted probe or
            # launch would have come good, and the slot goes straight back.
            failed = isinstance(exc, Exception)
            if failed and diagnostics:
                diagnostics.emit(
                    "pool.slot_error",
                    "Failed to start pooled Chromium",
                    {"slot_id": slot.slot_id, "error": type(exc).__name__},
                )
            self._retire_browser(slot)
            self._requeue(slot, diagnostics=diagnostics)
            if failed:
                return None
            raise

        if diagnostics:
            diagnostics.emit(
                "pool.acquire",
                "Acquired pooled Chromium slot",
                {"slot_id": slot.slot_id, "host": slot.host, "port": slot.port},
            )
        return slot

    async def release(self, slot: ChromiumSlot, *, diagnostics: Diagnostics | None) -> None:
        """Return a slot to the queue, and arm its idle close when one is configured.

        A coroutine because every caller awaits it; the work itself is
        synchronous, in :meth:`_requeue`.

        Args:
            slot: The slot being returned.
            diagnostics: Where to record the release, or ``None``.
        """
        self._requeue(slot, diagnostics=diagnostics)

    def _requeue(self, slot: ChromiumSlot, *, diagnostics: Diagnostics | None) -> None:
        """Put ``slot`` back on the queue and arm its idle close, without awaiting.

        :meth:`acquire` needs this from a path a cancellation may be running
        through, where an await is a place for that cancellation to land. The
        queue is unbounded, so ``put_nowait`` cannot refuse.

        Args:
            slot: The slot being returned.
            diagnostics: Where to record the release, or ``None``.
        """
        if diagnostics:
            diagnostics.emit(
                "pool.release",
                "Released pooled Chromium slot",
                {"slot_id": slot.slot_id, "host": slot.host, "port": slot.port},
            )
        self._arm_idle_close(slot)
        self.queue.put_nowait(slot)

    def _arm_idle_close(self, slot: ChromiumSlot) -> None:
        """Schedule closing ``slot``'s browser if it is still unused after the idle timeout.

        Nothing is scheduled when idle closing is off, or when the slot holds no
        running browser -- released after a failed start, after the recycle path
        terminated it, or after it exited on its own -- because there is nothing
        to close.

        Args:
            slot: The slot being released.
        """
        if self.idle_timeout_seconds is None or not _holds_running_browser(slot):
            return
        self._disarm_idle_close(slot)
        slot.idle_timer = asyncio.get_running_loop().call_later(
            self.idle_timeout_seconds, self._close_idle, slot
        )

    @staticmethod
    def _disarm_idle_close(slot: ChromiumSlot) -> None:
        """Cancel ``slot``'s pending idle close, if it has one.

        Args:
            slot: The slot whose timer to cancel.
        """
        if slot.idle_timer is not None:
            slot.idle_timer.cancel()
            slot.idle_timer = None

    def _close_idle(self, slot: ChromiumSlot) -> None:
        """Close ``slot``'s browser because it has sat unused for the idle timeout.

        This is the idle timer's callback, so ``slot`` is in the queue and no
        request holds it. A browser that exited on its own since the release is
        left for :meth:`ChromiumSlot.ensure_started` to relaunch, as it would be
        without this timer, so the next request is not told it was closed for
        being idle.

        Args:
            slot: The slot whose idle timer fired.
        """
        slot.idle_timer = None
        if not _holds_running_browser(slot):
            return
        self._retire_browser(slot)
        slot.idle_closed = True

    def _retire_browser(self, slot: ChromiumSlot) -> None:
        """Take ``slot``'s browser and profile off it, then terminate them in the background.

        Detaching happens here, synchronously, and the terminate only after. The
        obvious alternative -- awaiting ``slot.terminate()`` -- leaves
        ``slot.proc`` set until the process has exited, and an acquire that took
        the slot during that wait would health-probe a browser that is shutting
        down and could be handed it. Detached first, the next acquire finds
        ``proc`` is ``None`` and launches a fresh browser in a fresh profile
        directory, while the old one exits in its own.

        Args:
            slot: The slot to empty. Nothing happens if it holds neither a
                browser process nor a profile directory.
        """
        if slot.proc is None and slot.user_data_dir is None:
            return
        retired = ChromiumSlot(
            slot_id=slot.slot_id,
            host=slot.host,
            port=slot.port,
            proc=slot.proc,
            user_data_dir=slot.user_data_dir,
        )
        slot.proc = None
        slot.user_data_dir = None
        task = asyncio.get_running_loop().create_task(retired.terminate())
        self.retiring[task] = retired
        task.add_done_callback(self._forget_retired)

    def _forget_retired(self, task: asyncio.Task[None]) -> None:
        """Drop a completed terminate from :attr:`retiring`, and keep a cancelled one.

        asyncio's runner cancels every pending task when the event loop shuts
        down, before ``atexit`` runs :meth:`shutdown_sync`. A cancelled terminate
        may not have finished, and dropping it here would leave its browser to
        nobody.

        Args:
            task: The task that was terminating a detached browser.
        """
        if task.cancelled():
            return
        self.retiring.pop(task, None)

    async def shutdown(self) -> None:
        """Terminate every pooled browser, including any still being terminated in the background."""
        for slot in self.slots:
            self._disarm_idle_close(slot)
            await slot.terminate()
        for retired in list(self.retiring.values()):
            await retired.terminate()

    def shutdown_sync(self) -> None:
        """Terminate every pooled browser without an event loop, as at interpreter exit.

        Covers the browsers detached from their slot and not yet terminated,
        which are no longer in ``slots``.
        """
        for slot in self.slots:
            self._disarm_idle_close(slot)
            slot.terminate_sync()
        for retired in list(self.retiring.values()):
            retired.terminate_sync()


_POOL: ChromiumPool | None = None
_POOL_LOCK = asyncio.Lock()
_SHUTDOWN_REGISTERED = False


async def get_chromium_pool(diagnostics: Diagnostics | None = None) -> ChromiumPool:
    """Return the process-wide pool, creating it from the environment on first use.

    Args:
        diagnostics: Where to record the pool's creation, or ``None``.

    Returns:
        The one :class:`ChromiumPool` this process uses.
    """
    global _POOL
    if _POOL is not None:
        return _POOL
    async with _POOL_LOCK:
        if _POOL is None:
            _POOL = ChromiumPool(
                size=_resolve_pool_size(),
                acquire_timeout_seconds=_resolve_acquire_timeout_seconds(),
                port_range=_resolve_port_range(),
                idle_timeout_seconds=_resolve_idle_timeout_seconds(),
            )
            if diagnostics:
                diagnostics.emit(
                    "pool.init",
                    "Initialized Chromium pool",
                    {
                        "size": _POOL.size,
                        "port_range": _POOL.port_range,
                        "idle_timeout_seconds": _POOL.idle_timeout_seconds,
                    },
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
