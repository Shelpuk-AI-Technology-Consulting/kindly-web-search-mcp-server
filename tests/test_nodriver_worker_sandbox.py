"""Cover the nodriver worker's browser-startup orchestration.

`_fetch_html` runs in the *child* process and owns the sequence that brings a
browser up: pick a port, launch Chromium, wait for its DevTools endpoint,
connect nodriver to it, retry a transient failure, terminate the process of any
attempt that failed, and remove the temporary profile directory on the way out.
Those claims are about *sequencing and cleanup*, so no resolver test can make
them -- they stay at this layer permanently. The flag and default decisions that
used to be asserted here moved to
:mod:`tests.test_nodriver_worker_launch_resolvers`; nothing in this module should
assert one again.

**Every double is built with autospec**, from the real callable. That is the
whole point of this module's rewrite: the previous version patched each
collaborator with a bare ``AsyncMock``, which accepts any arguments at all, and
that is exactly why giving `_fetch_html` five new required keyword-only
arguments disabled eight tests at once without one of them objecting. An
autospec double raises :class:`TypeError` on a wrong-arity call, so the next
signature change fails here rather than passing.

Two details of that, both measured. Autospec of an ``async def`` runs its
signature check *inside* the coroutine body, so the check fires only when the
call is awaited -- fine for `_fetch_html`, which awaits all of them, but a
future assertion-only test would get no check at all. And the
`_terminate_process` call inside `_cleanup` sits under ``except Exception:
pass``, so a :class:`TypeError` raised there is swallowed: those tests still
fail, on the terminated-process comparison, but not with the signature error
that caused it.

**One double is weaker than the rest, and it is worth knowing which.**
:func:`nodriver.start` ends in ``**kwargs``, so its autospec accepts any keyword
at all -- measured. It catches surplus *positional* arguments and nothing else.
A nodriver release that renamed ``browser_executable_path`` would slip past this
module and fail only against a real browser. The three internal collaborators
(`_launch_chromium`, `_wait_for_devtools_ready`, `_terminate_process`) have no
``**kwargs`` and are fully checked.

**The real `nodriver` package is used, with only `nodriver.start` patched.** The
previous version installed a stand-in module built with
``type("X", (), {"start": ...})``. That could never have worked even with the
signature repaired: `_fetch_html` executes ``cdp = uc.cdp`` immediately after
importing, before any branch, and the stand-in has no ``cdp``. `nodriver` is an
unconditional runtime dependency of this project, so the real module is always
importable and there is nothing to gain by faking it.

Nothing here starts a browser, a subprocess or a socket, and every environment
variable `_fetch_html` reads is pinned -- including ``KINDLY_USER_AGENT``, whose
absence sends `_resolve_user_agent` into `_detect_chrome_version`, which runs
``<browser> --version`` as a real subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, create_autospec, patch

import nodriver

from kindly_web_search_mcp_server.scrape import nodriver_worker

#: Every environment variable `_fetch_html` and the resolvers it calls consult.
#: Cleared before each case so a case declares the whole of its own input. Four
#: of them are browser paths that CI images and developers commonly export, and
#: ``KINDLY_USER_AGENT`` is the one whose *absence* costs a real subprocess.
#:
#: This is not the whole of the ambient state, and the harness does not pretend
#: it is: `os.geteuid`, `shutil.which` and the module's diagnostics global are
#: pinned separately below, because none of them is an environment variable.
#: ``TMPDIR`` steers the real temporary directory and is deliberately left
#: alone -- the profile assertion reads a basename, so the location is free.
READ_ENVIRONMENT_VARIABLES = (
    "KINDLY_NODRIVER_SANDBOX",
    "KINDLY_NODRIVER_RETRY_ATTEMPTS",
    "KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS",
    "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER",
    "KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS",
    "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS",
    "KINDLY_USER_AGENT",
    "KINDLY_DIAGNOSTICS",
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "KINDLY_CHROME_PROXY",
    "KINDLY_CHROME_PROXY_BYPASS",
)

#: A browser path the tests hand to `_fetch_html` directly, so the executable
#: resolver short-circuits on its first branch and no `PATH` probe happens.
#: Deliberately not under ``/snap/``: `_is_snap_browser` keys on that prefix and
#: a snap browser multiplies the retry backoff by three, which would make the
#: timing of these tests depend on where the developer's Chromium came from.
BROWSER_PATH = "/usr/bin/chromium-for-tests"

#: A browser path that `_is_snap_browser` classifies as snap-packaged. It is
#: fabricated rather than the real `/snap/bin/chromium`, and that is worth a
#: sentence: on Ubuntu `/snap/bin/chromium` is a symlink to `/usr/bin/snap`, and
#: `_is_snap_browser` calls `os.path.realpath` first -- so it answers **False**
#: for the single most common way to have a snap Chromium (measured on Ubuntu
#: 24.04). A non-existent path under `/snap/` has no symlink to follow and is
#: classified correctly. The production defect that implies is reported, not
#: fixed here; a test that used the real path would silently assert the
#: non-snap branch, which is what the previous version of this file did.
SNAP_BROWSER_PATH = "/snap/bin/chromium-for-tests"

#: A ceiling on any single `_fetch_html` call, guarding a hang that makes no
#: progress -- an await that never resolves. Five seconds is fifty times the
#: ~0.1 s these cases take.
#:
#: It is **not** what catches a runaway retry loop; :data:`MAX_LAUNCHES` is.
#: Measured: with the retry loop mutated to spin forever, a wall-clock bound
#: alone took 47 s to report one failure against a 10 s budget, because every
#: doubled call is recorded and a loop with zero backoff records millions of
#: them. A timeout that fires and then takes five times its own budget to
#: unwind is the outcome a timeout exists to prevent.
PER_TEST_TIMEOUT_SECONDS = 5.0

#: The most browser launches any case here legitimately needs; the highest
#: attempt budget in this module is three. Exceeding it means the retry loop is
#: not terminating, and failing at once with that sentence is both faster and
#: more legible than waiting for a clock.
MAX_LAUNCHES = 10


class StubChromiumProcess:
    """A launched-Chromium stand-in, carrying only what its readers read.

    `_terminate_process` reads ``pid`` and ``returncode``;
    `_wait_for_devtools_ready` reads ``returncode`` to notice a browser that
    died before its endpoint came up. `_fetch_html` itself reads neither -- it
    holds the object and hands it to those two, both of which are doubled here
    -- so this stub exists to be *identifiable*, not to be exercised. Distinct
    pids are what let an assertion say which attempt's process was terminated
    rather than only how many terminations happened.

    It is a hand-written stub rather than
    ``create_autospec(asyncio.subprocess.Process)`` because that autospec
    supplies no ``pid`` at all: autospec copies a class's methods and
    class-level descriptors, not the attributes ``__init__`` assigns. The
    canonical typed definition of this shape belongs in production, and a later
    step in this epic puts it there; declaring a second one here would be the
    drift that step exists to prevent, so this stays a local stub with a
    comment rather than a competing contract.

    Args:
        pid: The process id to report.
    """

    def __init__(self, pid: int) -> None:
        """Record the identifying pid.

        Args:
            pid: The process id to report.
        """
        self.pid = pid
        self.returncode: int | None = None

    def __repr__(self) -> str:
        """Return a form that names the attempt in an assertion message."""
        return f"<StubChromiumProcess pid={self.pid}>"


def make_browser_double(
    html: str = "<html><body>ok</body></html>",
) -> tuple[Any, Any]:
    """Build a connected-browser double and the tab it hands out.

    Autospecced from the real :class:`nodriver.Browser` and
    :class:`nodriver.Tab` rather than hand-written, for the same reason as
    everything else here: a hand-written stand-in accepts any call, which is the
    defect this module was rewritten to remove. It also gets a detail right that
    a hand-written one had wrong -- ``Browser.stop`` is **synchronous** while
    ``Tab.get_content`` and ``Tab.close`` are coroutines (measured). `_cleanup`
    tolerates both by checking :func:`asyncio.iscoroutine` on the result, so a
    double with the wrong one would pass while misrepresenting the real object.

    Args:
        html: The document the tab returns from ``get_content``.

    Returns:
        The browser double and the tab double it returns from ``get``.
    """
    page = create_autospec(nodriver.Tab, instance=True)
    page.get_content.return_value = html
    browser = create_autospec(nodriver.Browser, instance=True)
    browser.get.return_value = page
    return browser, page


class Doubles:
    """The autospec doubles installed around one `_fetch_html` call.

    Attributes:
        launch: Stands in for `_launch_chromium`; returns a fresh
            :class:`StubChromiumProcess` per attempt.
        wait_ready: Stands in for `_wait_for_devtools_ready`.
        terminate: Stands in for `_terminate_process`.
        pick_port: Stands in for `_pick_free_port`, which otherwise binds a real
            socket.
        start: Stands in for :func:`nodriver.start`, the browser-connect call.
        temporary_directory: Wraps the real :class:`tempfile.TemporaryDirectory`
            so its keyword arguments can be inspected without losing the real
            create-and-remove behaviour.
        which: Stands in for :func:`shutil.which`, pinned to find nothing.
        recorder: A parent mock the four doubles are attached to, so their calls
            land in one ordered list.
        processes: Every process the launch double handed out, in order.
    """

    def __init__(self) -> None:
        """Create an empty record; :func:`orchestration_harness` fills it in."""
        self.launch: Any = None
        self.wait_ready: Any = None
        self.terminate: Any = None
        self.pick_port: Any = None
        self.start: Any = None
        self.temporary_directory: Any = None
        self.which: Any = None
        self.recorder: Any = MagicMock()
        self.processes: list[StubChromiumProcess] = []

    def call_sequence(self) -> list[str]:
        """Return the doubled calls in the order they actually happened.

        Per-double call *counts* cannot distinguish "each failed attempt was
        terminated before the next began" from "every process was terminated in
        one batch at the end" -- both give the same two numbers. Attaching the
        doubles to a shared parent records one interleaved sequence, which can.

        Returns:
            One name per call, in order, from :attr:`recorder`.
        """
        return [call[0] for call in self.recorder.mock_calls]

    def terminated_processes(self) -> list[Any]:
        """Return the process object of each `_terminate_process` call, in order.

        Returns:
            The first positional argument of every recorded call.
        """
        return [call.args[0] for call in self.terminate.call_args_list]


@contextlib.contextmanager
def orchestration_harness(
    *,
    environment: dict[str, str] | None = None,
) -> Iterator[Doubles]:
    """Install autospec doubles around everything `_fetch_html` would really do.

    Doubles exactly the four collaborators that would otherwise touch the
    machine -- launching Chromium, probing its DevTools port over HTTP, killing
    a process, and binding a socket to pick a port -- plus the browser-connect
    call. Everything else inside `_fetch_html` runs for real, which is the
    point: the orchestration is the subject.

    Every double is created with ``autospec=True``, so a call with the wrong
    arity raises :class:`TypeError` inside the code under test rather than being
    silently recorded. That is the property this whole module exists to restore.

    The real :class:`tempfile.TemporaryDirectory` still runs; it is only wrapped
    so the case can read the keyword arguments it was given. A mock in its place
    would make the profile-cleanup case assert against itself.

    ``asyncio.sleep`` is deliberately **not** patched here. The retry backoff is
    driven to zero through ``KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS``, a real
    seam; patching :func:`asyncio.sleep` would replace it on the global module
    for the duration and reach far past the code under test. Driving it to zero
    does **not** exercise the backoff arithmetic -- zero times anything is zero,
    and gutting the expression survives every case that uses this default. The
    one case that asserts the arithmetic patches :func:`asyncio.sleep` for
    itself and says so.

    The other sleep that remains is the fixed 100 ms `_cleanup` waits for
    Chromium to flush profile writes before the profile directory is removed.
    It is about 0.4 s of this module's runtime and is asserted by that same
    case; the rest pay it without observing it.

    :func:`shutil.which` is pinned to "nothing installed" even though four of
    the five cases hand `_fetch_html` an explicit executable and never reach the
    probe. The design states the rule and the reason: a case that leaves the
    real lookup in place is a case whose result depends on whether the developer
    has Chromium installed, and it would start depending on it the day the
    resolver consults ``PATH`` first.

    Args:
        environment: Variables to set for the duration, on top of a cleared
            slate. Anything in :data:`READ_ENVIRONMENT_VARIABLES` and not named
            here is removed.

    Yields:
        The installed :class:`Doubles`.
    """
    doubles = Doubles()
    # A cleared slate: every variable the worker reads is removed, then only
    # what the case asked for is set. Without this a developer's `CHROME_BIN`,
    # or a CI image's, steers the run.
    saved = {name: os.environ.get(name) for name in READ_ENVIRONMENT_VARIABLES}
    for name in READ_ENVIRONMENT_VARIABLES:
        os.environ.pop(name, None)
    os.environ["KINDLY_USER_AGENT"] = "kindly-test-agent"
    os.environ["KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS"] = "0"
    os.environ.update(environment or {})

    def _new_process(*_args: object, **_kwargs: object) -> StubChromiumProcess:
        """Hand out a distinctly-identified process for each launch.

        Returns:
            A stub process whose pid identifies the attempt that launched it.

        Raises:
            AssertionError: Once :data:`MAX_LAUNCHES` is exceeded, which means
                the retry loop is not terminating. Raised here rather than left
                to the wall-clock bound because the doubles record every call:
                a spinning loop is an unbounded sink that takes far longer to
                unwind than the bound it broke.
        """
        if len(doubles.processes) >= MAX_LAUNCHES:
            raise AssertionError(
                f"_fetch_html launched a browser more than {MAX_LAUNCHES} times; "
                "the retry loop is not terminating."
            )
        process = StubChromiumProcess(pid=9000 + len(doubles.processes))
        doubles.processes.append(process)
        return process

    real_temporary_directory = tempfile.TemporaryDirectory
    try:
        with (
            patch.object(nodriver_worker, "_launch_chromium", autospec=True) as launch,
            patch.object(nodriver_worker, "_wait_for_devtools_ready", autospec=True) as wait_ready,
            patch.object(nodriver_worker, "_terminate_process", autospec=True) as terminate,
            patch.object(nodriver_worker, "_pick_free_port", autospec=True) as pick_port,
            patch.object(nodriver, "start", autospec=True) as start,
            patch.object(
                nodriver_worker.tempfile,
                "TemporaryDirectory",
                autospec=True,
                side_effect=real_temporary_directory,
            ) as temporary_directory,
            patch.object(
                nodriver_worker.shutil, "which", autospec=True, return_value=None
            ) as which,
            # `_resolve_sandbox_enabled` and the failure message both read the
            # effective uid. Inert today -- the sandbox default is off either
            # way -- but the resolver module pins it for the same reason and a
            # container running as root would make it live the moment anything
            # here asserts a sandbox decision.
            patch.object(nodriver_worker.os, "geteuid", return_value=1000, create=True),
            # `_emit_diag` reads a module global, not the environment: clearing
            # `KINDLY_DIAGNOSTICS` does not reach it, because only the worker's
            # own entry point assigns it.
            patch.object(nodriver_worker, "_DIAG_ENABLED", False),
        ):
            launch.side_effect = _new_process
            pick_port.return_value = 9222
            doubles.launch = launch
            doubles.wait_ready = wait_ready
            doubles.terminate = terminate
            doubles.pick_port = pick_port
            doubles.start = start
            doubles.temporary_directory = temporary_directory
            doubles.which = which
            # Attaching to a shared parent is what makes the *order* of these
            # calls observable; it leaves each double's own arity checking
            # intact (measured).
            doubles.recorder.attach_mock(launch, "launch")
            doubles.recorder.attach_mock(wait_ready, "wait_ready")
            doubles.recorder.attach_mock(start, "connect")
            doubles.recorder.attach_mock(terminate, "terminate")
            yield doubles
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def fetch_html(**overrides: Any) -> str:
    """Call `_fetch_html` with the arguments a non-pooled fetch supplies.

    Written out rather than splatted from a dict so the call stays checkable:
    this module's subject is signature drift, and a helper that silences the
    type checker on its own call to the function under test would be
    self-defeating.

    Every call is bounded by :func:`asyncio.wait_for`. The design requires each
    test at this layer to carry a timeout shorter than the job's, and the
    function under test is a retry loop with sleeps in it: without a bound, a
    regression that stops terminating the loop hangs the suite instead of
    failing it. Ten seconds is far above the ~0.1 s these cases take and far
    below any plausible job timeout.

    The other anti-flake requirements at this layer do not bind here, because
    nothing real is started: there is no endpoint to hand-shake with, no port to
    allocate, no child whose pid must be reaped, and no child output to capture
    on failure. Those apply to the lifecycle tests that use a real fixture
    child.

    Args:
        **overrides: Replacements for the defaults below.

    Returns:
        The HTML `_fetch_html` produced.

    Raises:
        TimeoutError: If the call does not finish within the bound, which means
            the orchestration failed to terminate rather than failed an
            assertion.
    """
    kwargs: dict[str, Any] = {
        "referer": None,
        "user_agent": "kindly-test-agent",
        "wait_seconds": 0.0,
        "browser_executable_path": BROWSER_PATH,
        "reuse_browser": False,
        "remote_host": None,
        "remote_port": None,
        "user_data_dir": None,
        "overall_timeout_seconds": 30.0,
    }
    kwargs.update(overrides)
    url = kwargs.pop("url", "https://example.com")
    return await asyncio.wait_for(
        nodriver_worker._fetch_html(
            url,
            referer=kwargs["referer"],
            user_agent=kwargs["user_agent"],
            wait_seconds=kwargs["wait_seconds"],
            browser_executable_path=kwargs["browser_executable_path"],
            reuse_browser=kwargs["reuse_browser"],
            remote_host=kwargs["remote_host"],
            remote_port=kwargs["remote_port"],
            user_data_dir=kwargs["user_data_dir"],
            overall_timeout_seconds=kwargs["overall_timeout_seconds"],
        ),
        timeout=PER_TEST_TIMEOUT_SECONDS,
    )


class TestNodriverWorkerSandbox(unittest.IsolatedAsyncioTestCase):
    """Assert the browser-startup sequence `_fetch_html` owns."""

    async def test_devtools_probe_ignores_proxy_env(self) -> None:
        from kindly_web_search_mcp_server.scrape import nodriver_worker

        captured: dict[str, object] = {}

        class _Resp:
            status_code = 200

        class _AsyncClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, _url: str, timeout: float | None = None):
                return _Resp()

        fake_httpx = type("httpx", (), {"AsyncClient": _AsyncClient})

        class _Proc:
            returncode = None

        with patch.dict("sys.modules", {"httpx": fake_httpx}), patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://proxy.invalid:8080", "HTTPS_PROXY": "http://proxy.invalid:8080"},
            clear=False,
        ):
            await nodriver_worker._wait_for_devtools_ready(
                host="127.0.0.1",
                port=9222,
                proc=_Proc(),
                timeout_seconds=1.0,
            )

        self.assertIn("trust_env", captured)
        self.assertFalse(captured["trust_env"])

    async def test_uses_ignore_cleanup_errors_for_profile_dir(self) -> None:
        """Remove the temporary profile without letting its removal fail the request

        Chromium may still be flushing profile writes when the worker tears the
        directory down, so the request must not fail because a temp directory
        could not be deleted. Two halves: the flag that grants that tolerance is
        actually passed, and the directory really is created and really is gone
        afterwards. The real :class:`tempfile.TemporaryDirectory` runs -- a mock
        in its place would leave the second half asserting against itself.
        """
        browser, _page = make_browser_double()
        with orchestration_harness() as doubles:
            doubles.start.return_value = browser

            html = await fetch_html()

            executable, chromium_args = doubles.launch.call_args.args
            profile_flags = [a for a in chromium_args if a.startswith("--user-data-dir=")]

        self.assertIn("ok", html)
        kwargs = doubles.temporary_directory.call_args.kwargs
        self.assertIs(
            kwargs.get("ignore_cleanup_errors"),
            True,
            "The temporary profile directory must tolerate cleanup errors; "
            "without it a browser still flushing writes fails the request.",
        )
        self.assertEqual(kwargs.get("prefix"), "kindly-nodriver-")
        # The directory is only real if Chromium was pointed at it, and only
        # cleaned up if it is gone once the context manager has exited.
        self.assertEqual(len(profile_flags), 1, chromium_args)
        profile_dir = profile_flags[0].removeprefix("--user-data-dir=")
        # The port and the executable must reach all three collaborators as the
        # *same* values. Nothing else here checks that, and an independently
        # drawn mutation battery walked straight through it: connecting on a
        # port other than the one Chromium was launched on is exactly the fault
        # this module's retry loop exists to handle, and every case stayed green
        # against it.
        port = doubles.pick_port.return_value
        self.assertIn(f"--remote-debugging-port={port}", chromium_args)
        self.assertEqual(executable, BROWSER_PATH)
        self.assertEqual(doubles.wait_ready.call_args.kwargs["port"], port)
        self.assertEqual(doubles.start.call_args.kwargs["port"], port)
        self.assertEqual(
            doubles.start.call_args.kwargs["browser_executable_path"], BROWSER_PATH
        )
        self.assertTrue(os.path.basename(profile_dir).startswith("kindly-nodriver-"))
        self.assertFalse(
            os.path.exists(profile_dir),
            f"{profile_dir} outlived the fetch; the profile directory leaks.",
        )

    async def test_retries_on_failed_to_connect_to_browser(self) -> None:
        """Retry a transient connect failure and return the page from the retry

        The first attempt fails with the message nodriver raises when Chromium
        is up but not yet accepting DevTools connections -- the case the retry
        loop exists for. The second succeeds, and the caller sees HTML rather
        than the failure.

        The attempt budget is **three**, deliberately, while only two attempts
        are expected. With a budget of two the second attempt is also the last,
        so the loop ends whether or not it stops on success -- and deleting the
        ``break`` after a successful connect changed nothing, measured. A spare
        attempt makes "stopped retrying" a distinct, observable fact from "ran
        out of attempts": without the break a third browser is launched.
        """
        browser, page = make_browser_double()
        with orchestration_harness(
            environment={"KINDLY_NODRIVER_RETRY_ATTEMPTS": "3"}
        ) as doubles:
            doubles.start.side_effect = [
                RuntimeError("Failed to connect to browser"),
                browser,
            ]

            html = await fetch_html()

        self.assertIn("ok", html)
        self.assertEqual(doubles.start.call_count, 2)
        self.assertEqual(
            doubles.launch.call_count,
            2,
            "a third launch means the loop kept going after a successful "
            "connect, which strands a browser process",
        )
        # Both processes are terminated and in launch order: the first attempt's
        # before the retry begins, the second attempt's by the final cleanup.
        # Comparing the objects rather than counting them is what distinguishes
        # "each attempt was cleaned up" from "one was cleaned up twice".
        self.assertEqual(doubles.terminated_processes(), doubles.processes)
        # `Browser.stop` is synchronous on the real class, so this is a plain
        # call assertion rather than an await assertion. `_cleanup` branches on
        # `asyncio.iscoroutine` and tolerates either, which is exactly why the
        # double has to be shaped like the real object rather than by hand.
        browser.stop.assert_called_once_with()
        page.close.assert_awaited_once_with()
        # The failed attempt is cleaned up *before* the retry, not batched with
        # the rest at the end. Counts cannot tell those apart; order can.
        self.assertEqual(
            doubles.call_sequence(),
            ["launch", "wait_ready", "connect", "terminate",
             "launch", "wait_ready", "connect", "terminate"],
        )

    async def test_retries_and_terminates_on_devtools_timeout(self) -> None:
        """Terminate every attempt whose DevTools endpoint never came up

        A Chromium that starts but never opens its endpoint is the leak this
        guards: without the terminate in the failure path each retry would strand
        a browser process, and the worker exits without reaping it.
        """
        with orchestration_harness(
            environment={"KINDLY_NODRIVER_RETRY_ATTEMPTS": "2"}
        ) as doubles:
            doubles.wait_ready.side_effect = RuntimeError(
                "DevTools endpoint did not become ready in time"
            )

            with self.assertRaisesRegex(
                RuntimeError, r"Failed to connect to browser after 2 attempt"
            ):
                await fetch_html()

        self.assertEqual(doubles.launch.call_count, 2)
        self.assertEqual(
            doubles.start.call_count,
            0,
            "the browser-connect call must not be reached when the endpoint "
            "never became ready",
        )
        self.assertEqual(doubles.terminated_processes(), doubles.processes)
        # Each attempt is terminated before the next is launched. An
        # implementation that reaped everything at the end would satisfy the
        # call counts above and still strand a browser between attempts.
        self.assertEqual(
            doubles.call_sequence(),
            ["launch", "wait_ready", "terminate", "launch", "wait_ready", "terminate"],
        )
        # A port is picked per attempt, not once for the run. Hoisting the pick
        # out of the loop would send a retry at the port the previous, still
        # dying browser is holding -- which is the failure it is retrying from.
        self.assertEqual(doubles.pick_port.call_count, 2)

    async def test_backoff_grows_with_each_attempt_and_scales_for_snap(self) -> None:
        """Wait longer after each failed attempt, and longer again for a snap browser

        The backoff is `base * 2**attempt * snap_multiplier`. Every other case
        here sets the base to zero, which makes the whole expression zero and
        leaves the arithmetic unasserted -- measured: gutting it survives them
        all. So this case sets a real base and reads the durations back.

        A snap-packaged Chromium is slower to open its DevTools endpoint, which
        is why it gets its own multiplier; the two factors are asserted together
        because one case can distinguish all three mutations -- dropping the
        exponent, dropping the multiplier, and dropping both.

        See :data:`SNAP_BROWSER_PATH` for why the snap path here is fabricated
        rather than the real `/snap/bin/chromium`: the real one is classified
        **non**-snap by the code under test, which is a production defect this
        case would otherwise have papered over.

        This is the only case that patches :func:`asyncio.sleep`. It is a global
        and the harness avoids it for that reason, but a duration cannot be
        observed without either replacing the clock or actually waiting 4.5 s.
        """
        recorded: list[float] = []

        async def _record(delay: float) -> None:
            recorded.append(delay)

        with orchestration_harness(
            environment={
                "KINDLY_NODRIVER_RETRY_ATTEMPTS": "3",
                "KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS": "0.5",
                "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER": "3",
            }
        ) as doubles:
            doubles.wait_ready.side_effect = RuntimeError(
                "DevTools endpoint did not become ready in time"
            )

            with (
                patch.object(nodriver_worker.asyncio, "sleep", _record),
                self.assertRaises(RuntimeError),
            ):
                await fetch_html(browser_executable_path=SNAP_BROWSER_PATH)

        self.assertEqual(doubles.launch.call_count, 3)
        # 0.5 * 2**0 * 3, then 0.5 * 2**1 * 3. There is no third: the last
        # attempt raises instead of backing off. The trailing 0.1 is the profile
        # flush `_cleanup` waits for before removing the directory.
        self.assertEqual(recorded, [1.5, 3.0, 0.1])

    async def test_does_not_retry_a_non_retryable_error(self) -> None:
        """Surface an unrecognised startup failure at once instead of retrying it

        The retry loop asks `_is_retryable_browser_connect_error` before trying
        again. Nothing exercised the false branch, so deleting that question
        would have turned every failure -- a bad profile, a missing shared
        library -- into three slow attempts and a message blaming the connection.
        The attempt budget is three here so a retry would be visible.
        """
        with orchestration_harness(
            environment={"KINDLY_NODRIVER_RETRY_ATTEMPTS": "3"}
        ) as doubles:
            doubles.start.side_effect = RuntimeError(
                "chromium exited with a corrupt profile"
            )

            with self.assertRaises(RuntimeError) as raised:
                await fetch_html()

        # This count is the load-bearing assertion. Deleting the retryable
        # check makes the loop try three times and still re-raise the *same*
        # exception object -- the attempts-exhausted rewrite fires only on a
        # message match, which a non-retryable error by construction fails --
        # so only the attempt count distinguishes the two behaviours.
        self.assertEqual(doubles.launch.call_count, 1)
        self.assertEqual(doubles.start.call_count, 1)
        self.assertEqual(doubles.terminated_processes(), doubles.processes)
        # The original diagnosis must reach the caller. Asserting the
        # attempts-exhausted wording is *absent* is the half that discriminates:
        # a retried-to-exhaustion run also raises RuntimeError.
        self.assertIn("corrupt profile", str(raised.exception))
        self.assertNotIn("Failed to connect to browser after", str(raised.exception))

    async def test_missing_browser_executable_names_the_override_variable(self) -> None:
        """Tell the user how to fix a missing browser instead of failing obscurely

        When nothing is configured and nothing is on ``PATH``, this message is
        the only actionable guidance anyone gets: the alternative is a failure
        from inside Chromium, or none at all. The resolver's half of this claim
        -- that it returns ``None`` -- is asserted at component level; the
        translation into advice happens here and is asserted here.
        """
        with (
            orchestration_harness() as doubles,
            self.assertRaises(RuntimeError) as raised,
        ):
            await fetch_html(browser_executable_path=None)

        # The resolver really did fall through to the `PATH` probe, rather than
        # this case having been satisfied by some earlier branch. Asserting the
        # pinned `shutil.which` returns None would only restate the fixture;
        # asserting the code under test consulted it is an observation.
        self.assertGreater(
            doubles.which.call_count, 0, "the resolver never probed PATH"
        )

        message = str(raised.exception)
        self.assertIn("KINDLY_BROWSER_EXECUTABLE_PATH", message)
        self.assertIn("Install Chromium", message)
        self.assertEqual(
            doubles.launch.call_count,
            0,
            "no process may be launched when no executable was found",
        )

    def test_worker_stdout_write_uses_utf8_bytes(self) -> None:
        """
        Regression: on Windows, sys.stdout may be configured with a legacy codepage (e.g., cp1252),
        so writing HTML as text can raise UnicodeEncodeError. The worker must emit UTF-8 bytes.
        """
        import io

        from kindly_web_search_mcp_server.scrape import nodriver_worker

        class _BadTextIO(io.TextIOBase):
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, _s: str) -> int:  # pragma: no cover
                raise UnicodeEncodeError("charmap", "x", 0, 1, "cannot encode")

        stream = _BadTextIO()
        payload = "Hello — 世界".encode("utf-8", errors="strict")
        nodriver_worker._safe_write_bytes(stream, payload)
        self.assertIn(b"Hello", stream.buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
