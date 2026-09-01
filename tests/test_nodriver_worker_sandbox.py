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
signature change fails loudly here.

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
from typing import Any, Protocol, runtime_checkable
from unittest.mock import create_autospec, patch

import nodriver

from kindly_web_search_mcp_server.scrape import nodriver_worker

#: Every environment variable `_fetch_html` and the resolvers it calls consult.
#: Cleared before each case so a case declares the whole of its own input. Four
#: of them are browser paths that CI images and developers commonly export, and
#: ``KINDLY_USER_AGENT`` is the one whose *absence* costs a real subprocess.
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
BROWSER_PATH = "/usr/bin/chromium-for-tests"


@runtime_checkable
class ChromiumProcess(Protocol):
    """The surface a launched Chromium process must present to the worker.

    `_terminate_process` reads ``pid`` and ``returncode``, and
    `_wait_for_devtools_ready` reads ``returncode`` to notice a browser that
    died before its endpoint came up. Both are doubled in this module, so
    nothing here exercises those reads -- the Protocol exists to keep the stub
    honest anyway, because a double that has silently lost an attribute its real
    collaborators need is a double that will keep passing after the code stops
    working.

    It is a Protocol rather than an autospec of
    :class:`asyncio.subprocess.Process` for a measured reason, pinned by
    :meth:`TestProcessDoubleShape.test_autospec_of_a_process_omits_its_streams`.
    """

    pid: int
    returncode: int | None


class StubChromiumProcess:
    """A launched-Chromium stand-in that satisfies :class:`ChromiumProcess`.

    Args:
        pid: The process id to report. Distinct per attempt in the retry cases,
            so an assertion can tell *which* attempt's process was terminated
            rather than only how many terminations happened.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def __repr__(self) -> str:
        """Return a form that names the attempt in an assertion message."""
        return f"<StubChromiumProcess pid={self.pid}>"


class StubPage:
    """A browser tab that returns fixed content.

    Args:
        html: The document `get_content` returns.
    """

    def __init__(self, html: str) -> None:
        self.html = html
        self.closed = False

    def get_content(self) -> str:
        """Return the page's HTML, the way nodriver's synchronous accessor does."""
        return self.html

    async def close(self) -> None:
        """Record that the worker closed this tab."""
        self.closed = True


class StubBrowser:
    """A connected browser that hands out one :class:`StubPage`.

    Args:
        html: The document every tab returns.
    """

    def __init__(self, html: str = "<html><body>ok</body></html>") -> None:
        self.html = html
        self.pages: list[StubPage] = []
        self.stopped = False

    async def get(self, _url: str) -> StubPage:
        """Open a tab and return it.

        Args:
            _url: Ignored; navigation targets are not this module's subject.

        Returns:
            A new stub tab.
        """
        page = StubPage(self.html)
        self.pages.append(page)
        return page

    async def stop(self) -> None:
        """Record that the worker stopped the browser."""
        self.stopped = True


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
        processes: Every process the launch double handed out, in order.
    """

    def __init__(self) -> None:
        self.launch: Any = None
        self.wait_ready: Any = None
        self.terminate: Any = None
        self.pick_port: Any = None
        self.start: Any = None
        self.temporary_directory: Any = None
        self.processes: list[StubChromiumProcess] = []

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

    ``asyncio.sleep`` is deliberately **not** patched. The retry backoff is
    driven to zero through ``KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS``, which is a
    real seam and exercises the real backoff arithmetic; patching
    :func:`asyncio.sleep` would replace it on the global module for the duration
    and reach far past the code under test. The one sleep that remains is the
    fixed 100 ms `_cleanup` waits for Chromium to flush profile writes, which is
    behaviour under test rather than a wait for a condition.

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
        """Hand out a distinctly-identified process for each launch."""
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
        ):
            launch.side_effect = _new_process
            pick_port.return_value = 9222
            doubles.launch = launch
            doubles.wait_ready = wait_ready
            doubles.terminate = terminate
            doubles.pick_port = pick_port
            doubles.start = start
            doubles.temporary_directory = temporary_directory
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

    Args:
        **overrides: Replacements for the defaults below.

    Returns:
        The HTML `_fetch_html` produced.
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
    return await nodriver_worker._fetch_html(
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
        with orchestration_harness() as doubles:
            doubles.start.return_value = StubBrowser()

            html = await fetch_html()

            _, chromium_args = doubles.launch.call_args.args
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
        browser = StubBrowser()
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
        self.assertTrue(browser.stopped, "the browser was left running")

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

        self.assertEqual(doubles.start.call_count, 1)
        self.assertEqual(doubles.launch.call_count, 1)
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
            patch.object(
                nodriver_worker.shutil, "which", autospec=True, return_value=None
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            await fetch_html(browser_executable_path=None)

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


class TestProcessDoubleShape(unittest.TestCase):
    """Pin the measured autospec gap that decides how a process is doubled."""

    def test_autospec_of_a_process_omits_its_streams(self) -> None:
        """Record that an autospec process has no stream attributes

        :func:`unittest.mock.create_autospec` copies a class's *methods* and its
        class-level descriptors, not the instance attributes ``__init__``
        assigns. On :class:`asyncio.subprocess.Process` that means ``stdout``,
        ``stderr``, ``stdin`` and ``pid`` are absent while ``returncode``, being
        a property on the class, survives.

        The design cites this when it says a process double must be pinned by a
        Protocol rather than by autospec, and a later step builds a typed fake
        on the same fact. Until now it was prose. A ``mock`` release that started
        declaring those attributes would make the reasoning obsolete silently;
        this notices.
        """
        double = create_autospec(asyncio.subprocess.Process, instance=True)

        for absent in ("stdout", "stderr", "stdin", "pid"):
            self.assertFalse(
                hasattr(double, absent),
                f"create_autospec(Process) now supplies {absent!r}; the reason "
                "this project doubles a process with a Protocol instead has "
                "changed and the design should be revisited.",
            )
        self.assertTrue(
            hasattr(double, "returncode"),
            "create_autospec(Process) no longer supplies 'returncode', which "
            "is a class-level property and was the one attribute it did carry.",
        )

    def test_the_process_stub_satisfies_the_protocol(self) -> None:
        """Keep the stub in step with the surface the worker's collaborators read"""
        self.assertIsInstance(StubChromiumProcess(pid=1), ChromiumProcess)


if __name__ == "__main__":
    unittest.main()
