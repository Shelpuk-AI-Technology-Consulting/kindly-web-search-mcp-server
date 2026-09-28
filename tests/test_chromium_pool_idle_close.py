"""Cover the pool's idle close: a released browser left unused is terminated.

`KINDLY_NODRIVER_BROWSER_IDLE_TIMEOUT_SECONDS` arms a timer on every release and
disarms it on the next acquire; if it fires, the slot's browser and profile
directory are detached from the slot and terminated in the background, and the
slot's next acquire launches a fresh browser. The cases pin the decisions that
make that safe, each of which a plausible edit would break:

* the timer is disarmed as soon as ``acquire`` has the slot, before it awaits
  anything else, so a browser a request holds is never closed under it;
* the browser is detached **before** it is terminated, so an acquire that lands
  while it is exiting starts a fresh one instead of probing the dying one;
* a browser still being terminated is reachable from ``shutdown_sync``, although
  it is no longer in ``slots`` -- including after the event loop's own shutdown
  has cancelled its terminate;
* a browser that exited on its own is not reported as closed for being idle;
* a slot whose launch fails or is cancelled goes back to the queue, awaiting
  nothing on the way -- a cold start the idle close makes routine, so a lost
  slot there stops being a rare event;
* with more than one slot and the idle close on, a running browser is handed
  out before an empty slot, the most recently released first, so light traffic
  keeps one browser warm and lets the spares age out instead of cold-starting
  every slot in turn; with it off, the order stays first in, first out;
* nothing is armed when the setting is off, which is the default.

Nothing here starts a browser, opens a socket or signals a process. The slot's
process is a small double with no pid, and the collaborators that would touch a
real browser -- the DevTools readiness probe, the terminate and the slot launch
-- are replaced. The timers are real asyncio timers with short delays;
waiting on one is a bounded poll for the state it produces, never a bare sleep.
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape import chromium_pool
from kindly_web_search_mcp_server.scrape import nodriver_worker as worker
from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

#: The variable under test. Removed before every case, so a developer who
#: exports it cannot change what the defaults case sees.
IDLE_TIMEOUT_VARIABLE = "KINDLY_NODRIVER_BROWSER_IDLE_TIMEOUT_SECONDS"

#: Short enough that a case waiting for the timer finishes in milliseconds.
FIRING_TIMEOUT_SECONDS = 0.01

#: Long enough that the timer cannot fire while a case is still running, for the
#: cases that assert on an armed timer rather than a fired one.
HOLDING_TIMEOUT_SECONDS = 3600.0

#: Bound on every wait for a timer's effect. Generous, because a slow CI runner
#: must not turn a correct close into a failure; a real defect fails at it too.
WAIT_BOUND_SECONDS = 5.0

#: A DevTools port for the doubled browser. Nothing listens on it: the probe
#: that would connect is replaced.
FAKE_PORT = 9222


class FakeBrowserProcess:
    """Stand in for a running Chromium process, with no pid to signal.

    ``pid`` is ``None`` so that even the real terminate helper, were it reached,
    could not signal a process group on the developer's machine.

    Attributes:
        returncode: ``None`` while "running", as :class:`asyncio.subprocess.Process`.
        pid: Always ``None``.
        terminate_calls: How many times :meth:`terminate` was called.
    """

    def __init__(self) -> None:
        """Start "running"."""
        self.returncode: int | None = None
        self.pid: int | None = None
        self.terminate_calls = 0

    def terminate(self) -> None:
        """Exit at once, as SIGTERM would, and count the call."""
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        """Exit at once, as SIGKILL would."""
        self.returncode = -9

    async def wait(self) -> int | None:
        """Return the exit status, which :meth:`terminate` has already set.

        Returns:
            The process's return code.
        """
        return self.returncode


class TerminateRecorder:
    """Replace ``worker._terminate_process``, recording which process it was given.

    Optionally blocks until released, so a case can observe the pool while a
    close is still in flight.

    Attributes:
        terminated: Every process passed in, in call order.
        gate: When set, each call waits for it before marking its process exited.
    """

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        """Record calls, blocking on ``gate`` when one is given.

        Args:
            gate: An event each call waits for, or ``None`` to return at once.
        """
        self.terminated: list[Any] = []
        self.gate = gate

    async def __call__(self, proc: Any, *, grace_seconds: float = 1.5) -> None:
        """Record ``proc``, wait for the gate if any, then mark ``proc`` exited.

        Args:
            proc: The process the pool asked to terminate.
            grace_seconds: Accepted for signature parity; unused.
        """
        self.terminated.append(proc)
        if self.gate is not None:
            await self.gate.wait()
        proc.returncode = -15


@pytest.fixture(autouse=True)
def pinned_ambient_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variable under test and replace the DevTools readiness probe.

    The probe is what a held slot's ``ensure_started`` runs against a live
    ``proc``; answering "ready" keeps every acquire of a doubled browser off the
    network.

    Args:
        monkeypatch: pytest fixture that scopes and reverses the changes.
    """
    monkeypatch.delenv(IDLE_TIMEOUT_VARIABLE, raising=False)

    async def ready(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "_wait_for_devtools_ready", ready)


@pytest.fixture
def profile_dirs() -> Iterator[Callable[[], tempfile.TemporaryDirectory[str]]]:
    """Hand out real profile directories and remove any a case left behind.

    Yields:
        A factory returning a new :class:`tempfile.TemporaryDirectory`.
    """
    made: list[tempfile.TemporaryDirectory[str]] = []

    def make() -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory(
            prefix="kindly-test-idle-close-", ignore_cleanup_errors=True
        )
        made.append(directory)
        return directory

    yield make
    for directory in made:
        directory.cleanup()


def _pool_with_running_browser(
    idle_timeout_seconds: float | None,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> tuple[chromium_pool.ChromiumPool, FakeBrowserProcess, str]:
    """Build a one-slot pool whose slot already holds a "running" browser.

    Args:
        idle_timeout_seconds: The pool's idle timeout, or ``None`` for off.
        profile_dirs: The fixture's directory factory.

    Returns:
        The pool, the slot's browser double, and its profile directory's path.
    """
    pool = chromium_pool.ChromiumPool(
        size=1,
        acquire_timeout_seconds=1.0,
        port_range=None,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    slot = pool.slots[0]
    proc = FakeBrowserProcess()
    slot.proc = proc  # type: ignore[assignment]
    slot.port = FAKE_PORT
    slot.user_data_dir = profile_dirs()
    return pool, proc, slot.user_data_dir.name


def _pool_of_two_running_browsers(
    idle_timeout_seconds: float | None,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> chromium_pool.ChromiumPool:
    """Build a two-slot pool whose slots both already hold a "running" browser.

    Args:
        idle_timeout_seconds: The pool's idle timeout, or ``None`` for off.
        profile_dirs: The fixture's directory factory.

    Returns:
        The pool.
    """
    pool = chromium_pool.ChromiumPool(
        size=2,
        acquire_timeout_seconds=1.0,
        port_range=None,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    for slot in pool.slots:
        slot.proc = FakeBrowserProcess()  # type: ignore[assignment]
        slot.port = FAKE_PORT
        slot.user_data_dir = profile_dirs()
    return pool


def _record_launches(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> list[chromium_pool.ChromiumSlot]:
    """Replace the slot launch with one that starts a double and records the slot.

    Args:
        monkeypatch: pytest fixture that scopes and reverses the change.
        profile_dirs: The fixture's directory factory.

    Returns:
        The list each launch appends its slot to, in launch order.
    """
    launched: list[chromium_pool.ChromiumSlot] = []

    async def fake_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        launched.append(self)
        self.proc = FakeBrowserProcess()  # type: ignore[assignment]
        self.port = FAKE_PORT
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", fake_start)
    return launched


async def _acquire(
    pool: chromium_pool.ChromiumPool, diagnostics: Diagnostics | None = None
) -> chromium_pool.ChromiumSlot:
    """Acquire the pool's slot, failing the case if none is handed out.

    Args:
        pool: The pool under test.
        diagnostics: Passed through to ``acquire``.

    Returns:
        The acquired slot.
    """
    slot = await pool.acquire(user_agent="test-agent", diagnostics=diagnostics)
    assert slot is not None, "the pool handed out no slot"
    return slot


async def _until(predicate: Callable[[], bool], what: str) -> None:
    """Wait, bounded, for ``predicate`` to hold.

    Args:
        predicate: The state the case is waiting for.
        what: Named in the failure message.

    Raises:
        AssertionError: If it does not hold within :data:`WAIT_BOUND_SECONDS`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_BOUND_SECONDS
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.001)


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "0", "-5", "ten", "nan", "inf", "-inf"],
)
def test_idle_close_is_off_unless_given_a_positive_finite_timeout(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    """Unset, empty, unparseable, non-positive and non-finite values all mean off.

    ``inf`` is here deliberately: "close after forever" is off, and must not reach
    ``call_later`` as a delay.
    """
    if raw is not None:
        monkeypatch.setenv(IDLE_TIMEOUT_VARIABLE, raw)
    assert chromium_pool._resolve_idle_timeout_seconds() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("600", 600.0), (" 2.5 ", 2.5), ("0.001", 0.001), ("1e6", 1_000_000.0)],
)
def test_a_positive_timeout_is_honoured_as_given(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    """A positive, finite value is used unclamped, in either direction."""
    monkeypatch.setenv(IDLE_TIMEOUT_VARIABLE, raw)
    assert chromium_pool._resolve_idle_timeout_seconds() == expected


async def test_the_process_pool_takes_its_timeout_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_chromium_pool`` wires the resolver into the pool it creates.

    The resolver cases above cannot see this: a pool built without the keyword
    would fall back to the dataclass default, off, and every one of them would
    still pass.
    """
    monkeypatch.setenv(IDLE_TIMEOUT_VARIABLE, "42")
    monkeypatch.setattr(chromium_pool, "_POOL", None)
    # A real registration would run `shutdown_sync` over this pool at interpreter
    # exit, long after the case is gone.
    monkeypatch.setattr(chromium_pool, "_register_shutdown", lambda _pool: None)

    pool = await chromium_pool.get_chromium_pool()

    assert pool.idle_timeout_seconds == 42.0


async def test_a_released_browser_is_closed_once_idle_for_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """The real timer fires, terminates the browser, and removes its profile."""
    recorder = TerminateRecorder()
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    pool, proc, profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)

    await pool.release(slot, diagnostics=None)
    await _until(lambda: recorder.terminated and not pool.retiring, "the idle close")

    assert recorder.terminated == [proc]
    assert slot.proc is None
    assert slot.user_data_dir is None
    assert not Path(profile).exists()
    assert slot.idle_closed is True
    assert slot.idle_timer is None


async def test_reacquiring_before_the_timeout_keeps_the_browser(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """An acquire disarms the pending close before it awaits anything after taking the slot.

    Asserted on the timer handle rather than by outwaiting a delay: a cancelled
    handle cannot fire, and a case that slept past the timeout would pass on a
    machine too slow to have reached it. The probe records the handle's state
    because a disarm moved *after* the health check would still leave it
    cancelled by the end, with the timer free to fire during the probe's await.
    """
    recorder = TerminateRecorder()
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    pool, proc, _profile = _pool_with_running_browser(HOLDING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    armed = slot.idle_timer
    assert armed is not None, "release armed no idle close"
    seen_by_probe: list[bool] = []

    async def probe(**_kwargs: Any) -> None:
        seen_by_probe.append(armed.cancelled())

    monkeypatch.setattr(worker, "_wait_for_devtools_ready", probe)

    again = await _acquire(pool)

    assert again is slot
    assert seen_by_probe == [True]
    assert slot.idle_timer is None
    assert slot.proc is proc
    assert recorder.terminated == []


async def test_nothing_is_armed_when_idle_close_is_off(
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """Off is the default, and a pool left at it arms no timer."""
    pool, proc, _profile = _pool_with_running_browser(None, profile_dirs)
    slot = await _acquire(pool)

    await pool.release(slot, diagnostics=None)

    assert chromium_pool.ChromiumPool(
        size=1, acquire_timeout_seconds=1.0, port_range=None
    ).idle_timeout_seconds is None
    assert slot.idle_timer is None
    assert slot.proc is proc


async def test_a_slot_with_no_browser_arms_nothing() -> None:
    """A slot released without a browser -- a failed start, a recycle -- has nothing to close."""
    pool = chromium_pool.ChromiumPool(
        size=1,
        acquire_timeout_seconds=1.0,
        port_range=None,
        idle_timeout_seconds=HOLDING_TIMEOUT_SECONDS,
    )
    slot = pool.queue.get_nowait()

    await pool.release(slot, diagnostics=None)

    assert slot.proc is None
    assert slot.idle_timer is None


async def test_the_next_acquire_launches_a_fresh_browser_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """After a close, the next acquire relaunches and reports the close exactly once.

    The report goes to the acquiring request's diagnostics because that request
    is the one paying the cold start; the request that released the slot has
    already finished by the time the timer fires.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    launched: list[FakeBrowserProcess] = []

    async def fake_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        fresh = FakeBrowserProcess()
        launched.append(fresh)
        self.proc = fresh  # type: ignore[assignment]
        self.port = FAKE_PORT
        # As the real `_start`: an existing profile directory is reused.
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", fake_start)
    pool, proc, _profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    await _until(lambda: slot.idle_closed and not pool.retiring, "the idle close")

    diagnostics = Diagnostics(request_id="next", enabled=True, stream=io.StringIO())
    relaunched = await _acquire(pool, diagnostics)

    assert relaunched.proc is not proc
    assert launched == [relaunched.proc]
    reports = [e for e in diagnostics.entries if e["stage"] == "pool.slot_idle_closed"]
    assert len(reports) == 1
    assert reports[0]["data"]["idle_timeout_seconds"] == FIRING_TIMEOUT_SECONDS
    assert relaunched.idle_closed is False

    # Held again straight away, so nothing closed in between: no second report.
    await pool.release(relaunched, diagnostics=None)
    later = Diagnostics(request_id="later", enabled=True, stream=io.StringIO())
    await _acquire(pool, later)
    assert not [e for e in later.entries if e["stage"] == "pool.slot_idle_closed"]


async def test_an_acquire_during_the_close_never_gets_the_closing_browser(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """While the old browser is still exiting, the slot already holds none.

    This is what detaching before terminating buys. Awaiting ``slot.terminate()``
    instead would leave ``slot.proc`` pointing at the exiting browser for the
    whole wait, and this acquire would health-probe it and could be handed it.
    """
    gate = asyncio.Event()
    recorder = TerminateRecorder(gate=gate)
    monkeypatch.setattr(worker, "_terminate_process", recorder)

    async def fake_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        self.proc = FakeBrowserProcess()  # type: ignore[assignment]
        self.port = FAKE_PORT
        # As the real `_start`: an existing profile directory is reused, so a
        # slot that kept its old one would relaunch into the directory the
        # retiring browser is about to delete.
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", fake_start)
    pool, proc, profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    await _until(lambda: bool(recorder.terminated), "the idle close to start")

    assert pool.retiring, "the close finished before the case could observe it"
    relaunched = await _acquire(pool)

    assert relaunched.proc is not proc
    assert relaunched.user_data_dir is not None
    assert relaunched.user_data_dir.name != profile

    gate.set()
    await _until(lambda: not pool.retiring, "the idle close to finish")
    assert not Path(profile).exists()


async def test_shutdown_sync_reaches_a_browser_still_being_closed(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A detached browser is out of ``slots``; interpreter-exit teardown must still find it."""
    gate = asyncio.Event()
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder(gate=gate))
    pool, proc, profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    await _until(lambda: bool(pool.retiring), "the idle close to start")

    pool.shutdown_sync()

    assert proc.terminate_calls == 1
    assert not Path(profile).exists()
    gate.set()
    await _until(lambda: not pool.retiring, "the idle close to finish")


async def test_async_shutdown_reaches_a_browser_still_being_closed(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """The async teardown terminates the detached browser too, not only ``slots``.

    The gate stays shut until shutdown has asked for the browser a second time:
    once from the idle close, once from shutdown. A shutdown that iterated only
    ``slots`` would never make the second request, and the wait would time out.
    """
    gate = asyncio.Event()
    recorder = TerminateRecorder(gate=gate)
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    pool, proc, _profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    await _until(lambda: bool(pool.retiring), "the idle close to start")

    shutdown = asyncio.create_task(pool.shutdown())
    await _until(
        lambda: recorder.terminated.count(proc) == 2,
        "shutdown to reach the detached browser",
    )
    gate.set()
    await shutdown
    await _until(lambda: not pool.retiring, "the idle close to finish")


def test_a_terminate_cut_short_by_loop_shutdown_is_left_for_shutdown_sync(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """The interpreter-exit path as it really runs: loop gone first, ``atexit`` after.

    asyncio's runner cancels every pending task when the loop shuts down, so a
    close still in flight has its terminate cancelled before ``shutdown_sync``
    runs. The case above calls ``shutdown_sync`` from inside a live loop and
    cannot see that; this one lets ``asyncio.run`` finish first.
    """
    held: dict[str, Any] = {}

    async def close_in_flight() -> None:
        gate = asyncio.Event()  # never set: the loop ends with the close pending
        monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder(gate=gate))
        pool, proc, profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
        slot = await _acquire(pool)
        await pool.release(slot, diagnostics=None)
        await _until(lambda: bool(pool.retiring), "the idle close to start")
        held.update(pool=pool, proc=proc, profile=profile)

    asyncio.run(close_in_flight())
    held["pool"].shutdown_sync()

    assert held["proc"].terminate_calls == 1
    assert not Path(held["profile"]).exists()


async def test_a_browser_that_exited_while_held_arms_nothing(
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A crashed browser is not an idle one: there is nothing for the timer to close."""
    pool, proc, _profile = _pool_with_running_browser(HOLDING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    proc.returncode = 1

    await pool.release(slot, diagnostics=None)

    assert slot.idle_timer is None


async def test_a_browser_that_exited_while_idle_is_not_reported_as_idle_closed(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """The timer leaves an exited browser to ``ensure_started``, and says nothing.

    Reporting ``pool.slot_idle_closed`` here would tell the next request that a
    browser which crashed on its own was closed for being idle.
    """
    recorder = TerminateRecorder()
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    pool, proc, profile = _pool_with_running_browser(FIRING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    assert slot.idle_timer is not None, "release armed no idle close"
    proc.returncode = 1

    await _until(lambda: slot.idle_timer is None, "the idle timer to fire")

    assert slot.idle_closed is False
    assert recorder.terminated == []
    assert slot.proc is proc
    assert slot.user_data_dir is not None and slot.user_data_dir.name == profile


async def test_a_slot_whose_launch_is_cancelled_goes_back_to_the_queue(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A caller's deadline landing mid-launch must not cost the pool its slot.

    The caller never received the slot, so its own ``finally`` cannot return it;
    with a pool of one, a slot lost here wedges every later request. Whatever the
    slot holds is terminated, since nothing can tell whether the interrupted
    launch would have come up. Idle closing is off here: the defect does not
    need it, it only makes the cold start that exposes it routine.
    """
    recorder = TerminateRecorder()
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    launching = asyncio.Event()
    never = asyncio.Event()
    half_started = FakeBrowserProcess()

    async def slow_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()
        self.proc = half_started  # type: ignore[assignment]
        self.port = FAKE_PORT
        launching.set()
        await never.wait()

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", slow_start)
    pool = chromium_pool.ChromiumPool(size=1, acquire_timeout_seconds=1.0, port_range=None)
    acquiring = asyncio.create_task(_acquire(pool))
    await asyncio.wait_for(launching.wait(), timeout=WAIT_BOUND_SECONDS)

    acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    assert pool.queue.qsize() == 1
    await _until(
        lambda: recorder.terminated == [half_started] and not pool.retiring,
        "the interrupted launch to be terminated",
    )
    assert pool.queue.get_nowait().proc is None


async def test_a_failed_launch_returns_its_slot_without_awaiting_the_terminate(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """``acquire`` returns ``None`` with the slot back in the queue while the terminate is still pending.

    Awaiting the terminate there was a window of up to the terminate's grace
    period in which a cancellation lost the slot: it landed in the handler, past
    the clause that would have put the slot back. The gate holds the terminate
    open, so an ``acquire`` that awaited it would not return at all.
    """
    gate = asyncio.Event()
    recorder = TerminateRecorder(gate=gate)
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    half_started = FakeBrowserProcess()

    async def failing_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()
        self.proc = half_started  # type: ignore[assignment]
        raise RuntimeError("DevTools never became ready")

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", failing_start)
    pool = chromium_pool.ChromiumPool(size=1, acquire_timeout_seconds=1.0, port_range=None)
    diagnostics = Diagnostics(request_id="failed", enabled=True, stream=io.StringIO())

    slot = await asyncio.wait_for(
        pool.acquire(user_agent="test-agent", diagnostics=diagnostics),
        timeout=WAIT_BOUND_SECONDS,
    )

    assert slot is None
    assert pool.queue.qsize() == 1
    # The terminate is a task, so it starts on the loop's next turn -- after
    # `acquire` has already returned, which is the point.
    await _until(lambda: recorder.terminated == [half_started], "the terminate to start")
    assert pool.retiring, "the terminate finished although its gate is shut"
    assert [e["stage"] for e in diagnostics.entries] == ["pool.slot_error", "pool.release"]
    gate.set()
    await _until(lambda: not pool.retiring, "the terminate to finish")
    assert pool.queue.get_nowait().proc is None


async def test_a_launch_that_fails_before_its_process_exists_still_loses_its_profile(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A failed launch discards the profile directory even when no process was started.

    The directory is created before the browser is launched, so a launch that
    fails in between leaves a slot holding a profile and no process. Discarding
    it keeps the next launch from starting in whatever the failed one left there.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    created: list[str] = []

    async def failing_start(
        self: chromium_pool.ChromiumSlot, *, user_agent: str, port_range: Any, diagnostics: Any
    ) -> None:
        if self.user_data_dir is None:
            self.user_data_dir = profile_dirs()
        created.append(self.user_data_dir.name)
        raise RuntimeError("No Chromium-based browser executable found.")

    monkeypatch.setattr(chromium_pool.ChromiumSlot, "_start", failing_start)
    pool = chromium_pool.ChromiumPool(size=1, acquire_timeout_seconds=1.0, port_range=None)

    assert await pool.acquire(user_agent="test-agent", diagnostics=None) is None
    await _until(lambda: not pool.retiring, "the profile to be discarded")

    queued = pool.queue.get_nowait()
    assert queued.user_data_dir is None
    assert created and not Path(created[0]).exists()


async def test_a_slot_whose_probe_is_cancelled_goes_back_to_the_queue(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A caller's deadline landing mid-probe of a warm browser must not cost the slot.

    The launch case above reaches the handler through ``_start``; this one
    reaches it through the health probe of a browser that is already running,
    the other await ``ensure_started`` makes. The warm browser is given up,
    since nothing can tell whether the probe would have passed.
    """
    recorder = TerminateRecorder()
    monkeypatch.setattr(worker, "_terminate_process", recorder)
    pool, proc, _profile = _pool_with_running_browser(None, profile_dirs)
    probing = asyncio.Event()
    never = asyncio.Event()

    async def hanging_probe(**_kwargs: Any) -> None:
        probing.set()
        await never.wait()

    monkeypatch.setattr(worker, "_wait_for_devtools_ready", hanging_probe)
    acquiring = asyncio.create_task(_acquire(pool))
    await asyncio.wait_for(probing.wait(), timeout=WAIT_BOUND_SECONDS)

    acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    assert pool.queue.qsize() == 1
    await _until(
        lambda: recorder.terminated == [proc] and not pool.retiring,
        "the interrupted probe's browser to be terminated",
    )
    assert pool.queue.get_nowait().proc is None


async def test_a_warm_slot_is_handed_out_before_one_closed_for_being_idle(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """With two slots, a request gets the browser still running, not the emptied slot.

    Handed the slot the timer emptied, the request would pay a cold start while a
    warm browser sat in the queue behind it -- which a first-in, first-out queue
    does, because the slot released longest ago is both at its head and the
    first to be closed.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    launched = _record_launches(monkeypatch, profile_dirs)
    pool = _pool_of_two_running_browsers(FIRING_TIMEOUT_SECONDS, profile_dirs)
    closing = await _acquire(pool)
    staying = await _acquire(pool)
    await pool.release(closing, diagnostics=None)
    await _until(lambda: closing.idle_closed and not pool.retiring, "the idle close")
    # Released after the close, with a timeout it cannot reach within the case.
    pool.idle_timeout_seconds = HOLDING_TIMEOUT_SECONDS
    await pool.release(staying, diagnostics=None)

    handed = await _acquire(pool)

    assert handed.slot_id == staying.slot_id
    assert launched == []


async def test_sequential_requests_are_served_by_one_browser(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """One request at a time reuses the browser just released, and the spare closes.

    A first-in, first-out queue alternates between the slots instead, so each
    one's idle time is the pool size times the gap between requests: with that
    product above the timeout, every request pays a cold start although the gap
    alone is below it.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    launched = _record_launches(monkeypatch, profile_dirs)
    pool = _pool_of_two_running_browsers(FIRING_TIMEOUT_SECONDS, profile_dirs)
    spare = await _acquire(pool)
    busy = await _acquire(pool)
    await pool.release(spare, diagnostics=None)
    # The busy slot's releases get a timeout it cannot reach within the case.
    pool.idle_timeout_seconds = HOLDING_TIMEOUT_SECONDS
    await pool.release(busy, diagnostics=None)

    handed = []
    for _ in range(4):
        slot = await _acquire(pool)
        handed.append(slot.slot_id)
        await pool.release(slot, diagnostics=None)
    await _until(lambda: spare.idle_closed and not pool.retiring, "the spare's idle close")

    assert handed == [busy.slot_id] * 4
    assert busy.idle_closed is False
    assert launched == []


async def test_a_browser_that_exited_while_queued_waits_behind_a_running_one(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A slot whose browser crashed after its release counts as empty, not warm.

    Its ``proc`` is still set, so a queue that asked only whether a slot has a
    process would hand it out first, and the request would pay a relaunch while
    a running browser waited.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    launched = _record_launches(monkeypatch, profile_dirs)
    pool = _pool_of_two_running_browsers(HOLDING_TIMEOUT_SECONDS, profile_dirs)
    warm = await _acquire(pool)
    crashed = await _acquire(pool)
    await pool.release(warm, diagnostics=None)
    await pool.release(crashed, diagnostics=None)
    crashed.proc.returncode = -9  # type: ignore[union-attr]

    handed = await _acquire(pool)

    assert handed.slot_id == warm.slot_id
    assert launched == []


async def test_with_no_running_browser_queued_the_oldest_slot_goes_first(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """Among empty slots the order is first in, first out: a fresh pool starts at slot 0.

    Every empty slot costs the same cold start, so this only keeps slot ids in
    diagnostics as predictable as they were before the idle close existed.
    """
    launched = _record_launches(monkeypatch, profile_dirs)
    pool = chromium_pool.ChromiumPool(
        size=2,
        acquire_timeout_seconds=1.0,
        port_range=None,
        idle_timeout_seconds=HOLDING_TIMEOUT_SECONDS,
    )

    first = await _acquire(pool)

    assert first.slot_id == 0
    assert [slot.slot_id for slot in launched] == [0]


async def test_with_idle_close_off_free_slots_keep_first_in_first_out_order(
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A pool that does not set the idle timeout hands out slots as it always has.

    The warm-first order exists so that idle closes trim the spare slots. Without
    them it would only change which browser serves a request, for every pool of
    more than one slot, including those that never opted into the setting.
    """
    pool = _pool_of_two_running_browsers(None, profile_dirs)
    first = await _acquire(pool)
    second = await _acquire(pool)
    await pool.release(first, diagnostics=None)
    await pool.release(second, diagnostics=None)

    handed = []
    for _ in range(4):
        slot = await _acquire(pool)
        handed.append(slot.slot_id)
        await pool.release(slot, diagnostics=None)

    assert handed == [first.slot_id, second.slot_id] * 2


async def test_a_slot_released_without_a_browser_waits_behind_a_warm_one(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
) -> None:
    """A slot released empty is not handed out while a running browser is queued.

    This is the restart path in ``fetch_html_via_nodriver``: it terminates a
    stale slot, releases it and acquires again at once. A plain last-in,
    first-out queue would hand back the slot just emptied and cold-start it
    while a warm browser waited.
    """
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    launched = _record_launches(monkeypatch, profile_dirs)
    pool = _pool_of_two_running_browsers(HOLDING_TIMEOUT_SECONDS, profile_dirs)
    stale = await _acquire(pool)
    warm = await _acquire(pool)
    await pool.release(warm, diagnostics=None)

    await stale.terminate()
    await pool.release(stale, diagnostics=None)
    handed = await _acquire(pool)

    assert handed.slot_id == warm.slot_id
    assert launched == []


@pytest.mark.parametrize("variant", ["async", "sync"])
async def test_shutdown_disarms_a_pending_close(
    monkeypatch: pytest.MonkeyPatch,
    profile_dirs: Callable[[], tempfile.TemporaryDirectory[str]],
    variant: str,
) -> None:
    """Neither teardown leaves a timer behind to fire on a pool that is gone."""
    monkeypatch.setattr(worker, "_terminate_process", TerminateRecorder())
    pool, _proc, _profile = _pool_with_running_browser(HOLDING_TIMEOUT_SECONDS, profile_dirs)
    slot = await _acquire(pool)
    await pool.release(slot, diagnostics=None)
    armed = slot.idle_timer
    assert armed is not None, "release armed no idle close"

    if variant == "async":
        await pool.shutdown()
    else:
        pool.shutdown_sync()

    assert armed.cancelled()
    assert slot.idle_timer is None
