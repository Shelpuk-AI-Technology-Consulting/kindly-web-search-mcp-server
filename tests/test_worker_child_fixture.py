"""Smoke tests for the fixture child process the worker-lifecycle tests drive.

``tests/child_processes/worker_child.py`` is a test-only stand-in for the nodriver
worker. It exists because the parent-side subprocess machinery in
``scrape/worker_runner.py`` can only be exercised against a real process, and the
inline ``python -c`` payloads that machinery is tested with today cannot hang on
command, cannot build a process tree, and cannot emit a realistic ``KINDLY_DIAG``
frame without becoming an unreadable one-line string literal.

An instrument needs its own calibration. These cases are that calibration: each
drives one of the script's flags against a real process and asserts what it
produced. They deliberately do **not** drive ``_run_worker_command`` — proving
the runner's lifecycle is the later subsystem step this fixture unblocks, and a
smoke test that asserted both could not tell a broken fixture from a broken
runner.

The one claim borrowed from production is the decoder. A frame the fixture emits
is fed to the shipped ``worker_runner._consume_stderr_line``, because "emits
known frames" means "emits frames the real parent accepts", and a comparison of
field names against the worker's emitter would pass on a frame no decoder reads.

**Readiness is polled, never timed.** Every case waits for the script's readiness
frame under a hard 30-second deadline and prints the startup duration as
telemetry. No case asserts a startup budget: a millisecond threshold measures a
loaded CI runner and an antivirus scanner's process-start delay, not this code.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import pathlib
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape.worker_runner import (
    _consume_stderr_line,
    _StderrAccumulator,
)

#: The script under test, addressed by path rather than imported. It is not part
#: of any package and is never imported by the suite -- that is the point of it.
#:
#: The directory is named for what it holds, matching ``tests/doubles/`` and
#: ``tests/typing_negative/``. Not ``tests/fixtures/``: in a pytest suite that
#: word already means ``@pytest.fixture``, and a directory of spawnable scripts
#: is not that.
FIXTURE_CHILD = (
    Path(__file__).resolve().parent / "child_processes" / "worker_child.py"
)

#: The frame prefix both the real worker and this fixture write, as bytes,
#: because stderr is read undecoded here so the invalid-UTF-8 case survives.
FRAME_PREFIX = b"KINDLY_DIAG "

#: Hard ceiling on waiting for the readiness frame. Thirty seconds is not a
#: measurement of anything -- it is far longer than a Python interpreter has ever
#: needed to start, chosen so that the deadline only ever fires on a script that
#: is genuinely not going to announce itself.
READINESS_TIMEOUT_SECONDS = 30.0

#: Ceiling on reaping a child during teardown. Shorter than the readiness budget
#: because by this point the process has already been signalled.
REAP_TIMEOUT_SECONDS = 10.0

#: How long the hang case waits to prove the child is *still there*. A liveness
#: bound, and the inverse of a startup budget: it fails only if the child dies,
#: never if the machine is slow, so it cannot become the flake generator section
#: 5.2a warns about. Short because the claim needs no longer.
HANG_LIVENESS_SECONDS = 0.5

#: Bounded moment teardown gives a child that is already exiting, before
#: deciding it has to be killed.
EXIT_GRACE_SECONDS = 0.5

#: The stage the readiness frame carries.
READY_STAGE = "fixture.ready"

#: Payload the stdout case asks for. Carries a newline, which text-mode writing
#: would rewrite on Windows, and a non-ASCII character, which a console codepage
#: would re-encode -- both silent on Linux, so both are built into the literal
#: rather than left to a future reader to remember.
WORKER_STDOUT = "<html><body><p>ok ✓</p>\n</body></html>"

#: The non-frame line the garbage mode writes. Named here because the case
#: asserts it reached the stderr tail, and the script and the assertion would
#: otherwise drift apart silently.
GARBAGE_PLAIN_LINE = "chrome: ordinary noise on stderr"


def _child_environment() -> dict[str, str]:
    """Build the environment for a fixture child.

    Inherits the parent's environment rather than starting from empty, matching
    ``tests/test_worker_runner.py``. An empty environment is not the more
    hermetic choice on every platform: Windows needs ``SYSTEMROOT`` to start a
    process at all, and these cases are portable.

    Returns:
        A copy of the current environment, safe to hand to a child.
    """
    return dict(os.environ)


def _kill_pid(pid: int) -> None:
    """Kill one process by pid, tolerating its having already exited.

    Keyed on a pid this test spawned, never on a process name: a name scan is
    vulnerable to pid reuse and would reach a developer's own processes.

    Args:
        pid: The process to kill.
    """
    # `SIGKILL` does not exist on Windows and `Popen.kill` is not available for a
    # pid this process does not own as a `Popen`, so the platforms diverge here.
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=REAP_TIMEOUT_SECONDS,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    # Bounded and swallowed because this runs in `finally`: a `taskkill` stalled
    # behind a scanner must fail the case that is already failing, not hang the
    # suite in teardown.
    except (OSError, subprocess.TimeoutExpired):
        return


def _pid_is_alive(pid: int) -> bool:
    """Report whether a pid names a running process.

    Args:
        pid: The process to probe.

    Returns:
        ``True`` while the process is running. "Running" is exact on Windows and
        on Linux; on other POSIX platforms it degrades to "exists in the process
        table", because the signal-0 probe alone cannot exclude a zombie and
        there is no ``/proc`` to consult. A ``False`` for a direct child is only
        meaningful once that child has been waited for.
    """
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                check=False,
                text=True,
                timeout=REAP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A signal-0 probe succeeds against a zombie, so on Linux the process state
    # is read as well. Cheap -- one `open` -- and it is what lets the descendant
    # case mean "running" rather than "exists in the process table". `/proc` is
    # Linux-only; on other POSIX platforms the probe stays as strong as it was.
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    # Field three, after the comm field, which is parenthesised and may itself
    # contain spaces -- so split after the last ")" rather than on whitespace.
    _, _, rest = stat.rpartition(")")
    return rest.split()[0] != "Z"


def _pump_lines(stream: Any, sink: queue.Queue[bytes | None]) -> None:
    """Move complete lines off a pipe onto a queue until the pipe closes.

    A reader thread rather than a blocking read in the test body: a pipe read has
    no timeout, so a script that never speaks would hang the suite instead of
    failing it. The queue's own ``get`` timeout is what turns that into a
    deadline.

    Args:
        stream: The child's standard error, opened in binary mode.
        sink: Queue receiving each line with its terminator, then ``None`` at
            end of file so a waiting reader learns the stream closed.
    """
    for line in iter(stream.readline, b""):
        sink.put(line)
    sink.put(None)


def _pump_all(stream: Any, sink: list[bytes]) -> None:
    """Drain a pipe to end of file into a single-element list.

    Drained on a thread even when a case ignores the result, because a child that
    fills its stdout pipe while nobody reads blocks forever.

    Args:
        stream: The child's standard output, opened in binary mode.
        sink: List the whole payload is appended to once the stream closes.
    """
    sink.append(stream.read())


@dataclass
class _RunningChild:
    """A spawned fixture child and the machinery draining its pipes.

    Attributes:
        proc: The process handle.
        ready: The decoded readiness frame, once awaited.
        stderr_lines: Queue of complete stderr lines, ``None`` marking the end.
        argv: The full command the child was spawned with, quoted in failures.
        ready_line: The raw readiness line, kept so a case can route it through
            the production decoder along with the frames that followed it.
        stdout_sink: Single-element list receiving the whole stdout payload.
        killed_while_running: Whether teardown had to kill a child that had not
            finished, which makes its stdout payload untrustworthy.
    """

    proc: subprocess.Popen[bytes]
    stderr_lines: queue.Queue[bytes | None]
    argv: list[str] = field(default_factory=list)
    ready_line: bytes = b""
    stdout_sink: list[bytes] = field(default_factory=list)
    ready: dict[str, Any] = field(default_factory=dict)
    killed_while_running: bool = False

    @property
    def grandchild_pid(self) -> int | None:
        """The pid the readiness frame reported for a spawned descendant.

        Returns:
            The descendant's pid, or ``None`` when none was spawned or the
            readiness frame has not been read yet.
        """
        value = self.ready.get("data", {}).get("grandchild_pid")
        return value if isinstance(value, int) else None

    def stdout_bytes(self) -> bytes:
        """The child's whole standard output, once the stream has closed.

        Returns:
            Every byte the child wrote to standard output.

        Raises:
            AssertionError: If the stream never closed, or if teardown had to
                kill a child that was still running. The second case is the
                interesting one: readiness is announced *before* the payload is
                written, so a case that leaves the ``with`` block as soon as the
                child is ready races teardown against the write and compares a
                truncated payload. Measured at three failures in forty runs
                before this check existed. A case that wants the payload must
                wait for the child to exit; this turns forgetting into a
                sentence rather than into a flake.
        """
        assert not self.killed_while_running, (
            "teardown killed this child while it was still running, so its "
            "stdout may be truncated -- wait for it to exit inside the `with` "
            f"block before reading the payload.{self.captured_report()}"
        )
        assert self.stdout_sink, "the child's stdout has not been drained yet"
        return self.stdout_sink[0]

    def wait_for_exit(self, timeout: float = REAP_TIMEOUT_SECONDS) -> int:
        """Wait for a child that is expected to finish on its own.

        Args:
            timeout: Seconds to wait.

        Returns:
            The child's exit code.

        Raises:
            AssertionError: If the child is still running at the deadline.
        """
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"the fixture child was still running after {timeout}s when it "
                f"was expected to exit.{self.captured_report()}"
            ) from None

    def next_stderr_line(self, timeout: float) -> bytes:
        """Take the next complete stderr line, waiting up to ``timeout``.

        Waits in slices rather than in one long block so that a child which has
        *died* is reported as dead within a slice instead of after the whole
        deadline. The two failures need different fixes and a shared message
        would send the next reader the wrong way: a child that is slow to start
        is a runner problem, a child that exited is a bad command line -- and the
        second is the common case, because a case is written before the flag it
        drives and therefore first meets an ``argparse`` that exits 2.

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            One line, including its terminator.

        Raises:
            AssertionError: If the deadline passes with no line, or the child
                exited, or its stream closed. The message names which, and
                quotes whatever the child managed to say.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"the fixture child wrote no stderr line within {timeout}s "
                    f"and is still running.{self.captured_report()}"
                )
            try:
                line = self.stderr_lines.get(timeout=min(0.1, remaining))
            except queue.Empty:
                # An exited child will never produce the awaited line, so there
                # is nothing to gain by waiting out the rest of the deadline.
                if self.proc.poll() is not None:
                    raise AssertionError(
                        f"the fixture child exited with code {self.proc.returncode} "
                        f"before writing the expected stderr line."
                        f"{self.captured_report()}"
                    ) from None
                continue
            if line is None:
                raise AssertionError(
                    "the fixture child's stderr closed before the expected line "
                    f"(exit code {self.proc.poll()}).{self.captured_report()}"
                )
            return line

    def captured_report(self) -> str:
        """Render whatever the child has said so far, for a failure message.

        Section 5.4 requires child output to be captured and attached on
        failure. Without it every failure here reads as "the child did not do
        what was expected" with no way to see what it did instead, and the child
        is a separate process whose output pytest never shows.

        Returns:
            A block naming the argv and quoting the captured streams, ready to
            append to an assertion message.
        """
        seen: list[bytes] = []
        # Non-blocking: this runs on a failure path and must not add a second
        # wait to a case that is already failing.
        while True:
            try:
                line = self.stderr_lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            seen.append(line)
        stdout = self.stdout_sink[0] if self.stdout_sink else b"<still open>"
        return (
            f"\n  argv:   {self.argv}"
            f"\n  stderr: {b''.join(seen)!r}"
            f"\n  stdout: {stdout!r}"
        )

    def drain_stderr(self, timeout: float) -> list[bytes]:
        """Collect every remaining stderr line until the stream closes.

        Args:
            timeout: Seconds to wait for each individual line.

        Returns:
            The remaining lines, in order.
        """
        lines: list[bytes] = []
        while True:
            try:
                line = self.stderr_lines.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(
                    f"the fixture child's stderr did not close within {timeout}s"
                ) from None
            if line is None:
                return lines
            lines.append(line)


def _decode_frame(line: bytes) -> dict[str, Any]:
    """Decode one ``KINDLY_DIAG`` line into its payload object.

    Args:
        line: A complete stderr line, with or without its terminator.

    Returns:
        The decoded frame.

    Raises:
        AssertionError: If the line is not a frame, or its payload is not a JSON
            object. Both carry the offending line so a failure is readable.
        json.JSONDecodeError: If the payload will not decode at all. Left to
            propagate rather than converted, because the caller that matters
            catches it beside ``AssertionError`` and adds the child's exit code
            -- this is the path an ``argparse`` usage message takes.
    """
    assert line.startswith(FRAME_PREFIX), f"not a diagnostic frame: {line!r}"
    payload = line[len(FRAME_PREFIX) :].strip()
    parsed = json.loads(payload)
    assert isinstance(parsed, dict), f"frame payload is not an object: {payload!r}"
    return parsed


def _route_through_real_decoder(lines: list[bytes]) -> _StderrAccumulator:
    """Feed captured stderr lines through the parent's own line router.

    Reproduces what ``worker_runner._read_stderr_stream`` does around
    ``_consume_stderr_line``: decode with undecodable bytes **replaced** rather
    than raised, split on newlines, and strip the carriage return. The
    replacement half is not a convenience -- it is the behaviour the invalid-UTF-8
    case exists to check, and decoding strictly here would raise inside the test
    and report a crash that production does not have.

    Args:
        lines: Complete stderr lines as captured, terminators included.

    Returns:
        The accumulator after every line has been routed.
    """
    state = _StderrAccumulator()
    for line in lines:
        text = line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        _consume_stderr_line(state, text, tail_limit=10_000)
    return state


@contextlib.contextmanager
def _fixture_child(*args: str) -> Iterator[_RunningChild]:
    """Spawn the fixture child, wait for readiness, and reap it afterwards.

    Readiness is a frame on stderr rather than a sleep, and the wait is bounded
    by :data:`READINESS_TIMEOUT_SECONDS`. The elapsed time is printed rather than
    asserted -- see this module's docstring for why.

    **Both pipes are drained, on threads, always.** Not tidiness: the script
    writes its stdout payload *before* it hangs, so a caller that drained only
    stderr would block the child inside that write as soon as the payload passed
    the pipe capacity -- 64 KiB on Linux, and an unspecified and much smaller
    figure on Windows, where the documentation promises only "the default buffer
    size". Every later assertion about a child that is "still hanging" would then
    pass for the wrong reason.

    Teardown kills the descendant before the child, so the descendant cannot be
    reparented and missed, and it keys on the pids this call spawned. The
    descendant's pid is one the *child* reported rather than one this process
    spawned, which section 5.4's rule does not literally cover: the protection
    against signalling a stranger after a pid is recycled is that the descendant
    is killed while its reporting parent is still alive, and the descendant does
    not exit on its own within any test's lifetime.

    On failure the child's captured output is attached to the error, because the
    child is a separate process whose output pytest otherwise never shows.

    Args:
        *args: Flags for the script, after its path.

    Yields:
        The running child, with its readiness frame already decoded.
    """
    started = time.monotonic()
    argv = [sys.executable, str(FIXTURE_CHILD), *args]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_environment(),
    )
    child = _RunningChild(proc=proc, stderr_lines=queue.Queue(), argv=argv)
    # The `try` opens the moment the process exists, not once it is fully set
    # up. Everything between those two points can still fail -- `Thread.start`
    # raises under thread exhaustion -- and a failure there leaves a running
    # child with nothing to reap it but its own 300s backstop. Measured, by
    # forcing `Thread.start` to raise: with the `try` opening after the threads
    # start, the child is still alive once the helper has unwound; opening it
    # here, it is gone.
    threads: list[threading.Thread] = []
    try:
        assert proc.stdout is not None and proc.stderr is not None
        threads = [
            threading.Thread(
                target=_pump_lines, args=(proc.stderr, child.stderr_lines), daemon=True
            ),
            threading.Thread(
                target=_pump_all, args=(proc.stdout, child.stdout_sink), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()

        # The readiness frame is the script's first output by contract, so this
        # both waits for startup and asserts that ordering in one step.
        #
        # Wrapped in its own handler because the interesting failure is not the
        # timeout, it is the *chatty death*: a child given an unknown flag has
        # `argparse` write usage to stderr and then exit 2, so the line arrives,
        # the exit-code branch in `next_stderr_line` is never reached, and
        # without this the report would name neither the exit code nor the argv.
        first = child.next_stderr_line(READINESS_TIMEOUT_SECONDS)
        child.ready_line = first
        try:
            child.ready = _decode_frame(first)
            assert child.ready.get("stage") == READY_STAGE, (
                f"the first stderr frame was {child.ready.get('stage')!r}, "
                f"not {READY_STAGE!r}"
            )
        except (AssertionError, json.JSONDecodeError) as failure:
            # A child that has already exited did not merely emit the wrong
            # frame -- it died talking, which is a different diagnosis. Waited
            # for briefly first: `argparse` writes its usage and *then* exits,
            # so polling the instant the line arrives would often report a
            # still-running child and lose the exit code that names the cause.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=EXIT_GRACE_SECONDS)
            code = proc.poll()
            died = "" if code is None else f" The child had exited with code {code}."
            raise AssertionError(
                f"the fixture child's first stderr line is not a readiness "
                f"frame: {failure}{died}{child.captured_report()}"
            ) from failure
        print(
            f"fixture child readiness: "
            f"{int((time.monotonic() - started) * 1000)} ms (telemetry, not asserted)"
        )
        try:
            yield child
        except AssertionError as failure:
            # Section 5.4 requires child output on failure. Re-raised with the
            # capture appended rather than printed, so it travels with the
            # assertion into the report instead of into captured stdout that a
            # passing-but-noisy run would bury.
            raise AssertionError(f"{failure}{child.captured_report()}") from failure
    finally:
        # Descendant first: killing the child would reparent it, and a pid read
        # from the readiness frame is the only handle this test has on it.
        grandchild = child.grandchild_pid
        if grandchild is not None:
            _kill_pid(grandchild)
        # A child that is on its way out is given a bounded moment first. Its
        # stderr reaching end of file does not mean it has exited, so a case
        # that drained the stream can arrive here microseconds early and record
        # a kill that did not need to happen.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=EXIT_GRACE_SECONDS)
        # Only signal a child that is still running. A child that has already
        # exited is a zombie until waited for, so its pid is still signallable --
        # and a case asserting the exit code it chose should not have to reason
        # about whether teardown replaced it.
        if proc.poll() is None:
            child.killed_while_running = True
            _kill_pid(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=REAP_TIMEOUT_SECONDS)
        for thread in threads:
            thread.join(timeout=REAP_TIMEOUT_SECONDS)
        # Only close a pipe whose pump has finished with it. Reachable when the
        # reap above timed out and a pump is still inside `read`; closing under
        # it there would raise in a thread nothing is watching.
        if all(not thread.is_alive() for thread in threads):
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def test_the_fixture_child_imports_only_the_standard_library() -> None:
    """Keep the fixture independent of the code it is used to test

    The reasons are about *startability*, not purity. The runner hands its child
    a **complete** environment rather than merging one into the parent's, so
    whether an import of the package resolved would depend on whatever path
    setup that environment happened to carry; and the suite reaches ``src/``
    only through the ``sys.path`` insertion in ``tests/conftest.py``, which a
    script invoked by path never executes. A third-party import would fail the
    same way, in any environment without that package.

    The argument this does **not** rest on -- and the script's own docstring
    says so too -- is that importing the code under test lets a defect hide. It
    is true in general and weak here, because the decoder case pins these frames
    to the production decoder anyway.

    Asserted over the module's own syntax tree rather than by running it, so the
    check holds for a branch that never executes.
    """
    tree = ast.parse(FIXTURE_CHILD.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no module name to attribute; the script is
            # not in a package, so one would be a bug in its own right.
            assert node.level == 0, "the fixture child must not import relatively"
            if node.module:
                roots.add(node.module.split(".")[0])

    assert roots, "the import sweep found nothing, so it is asserting nothing"
    outside = sorted(roots - set(sys.stdlib_module_names))
    assert not outside, f"the fixture child imports non-stdlib modules: {outside}"


def test_pytest_collects_nothing_from_the_child_process_directory() -> None:
    """Keep the fixture out of the suite that uses it

    ``tests/`` is a collection root, and a script placed there that pytest
    decided to import would be executed at collection time -- which, for this
    script, means writing frames onto the runner's stderr and possibly hanging.
    Nothing about the current file name invites that, and this case is what keeps
    it true if someone renames the script.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(Path(__file__).resolve().parents[1] / "pyproject.toml"),
            str(FIXTURE_CHILD.parent),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        env={
            **{k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"},
            "PYTHONIOENCODING": "utf-8",
        },
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    # Exit code 5 is "no tests collected", which is the expected outcome; the
    # substring check is what distinguishes it from a collection *error*.
    assert completed.returncode == 5, (
        f"expected an empty collection, got exit {completed.returncode}\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "error" not in completed.stdout.lower(), completed.stdout


@pytest.mark.subsystem
def test_the_first_stderr_frame_announces_readiness_and_the_pid() -> None:
    """Announce readiness on stderr, first, with the pid a harness must reap

    The readiness handshake is the whole reason a lifecycle test can avoid
    sleeping. Two things have to hold for it to be usable: the frame must arrive
    before anything else on stderr, so a reader can consume exactly one line
    without guessing, and it must be flushed, so it arrives while the script is
    still running rather than when it exits.

    Flushing is what this case really pins, and it is driven against a child
    that **hangs** for that reason alone. Standard error is unbuffered only when
    it is a terminal; behind a pipe it is block-buffered. Against a child that
    exits, an unflushed frame still arrives -- interpreter shutdown flushes the
    buffer -- so the case would pass with the flush deleted and would be pinning
    nothing. Measured: removing ``sys.stderr.buffer.flush()`` leaves a
    short-lived child's readiness frame arriving normally. Only a process that is
    still running can tell the two apart.

    The pid matters because a harness reaping a tree keys on pids it spawned, and
    the frame is where it learns them.
    """
    with _fixture_child("--hang") as child:
        assert child.ready["stage"] == READY_STAGE
        assert child.ready["data"]["pid"] == child.proc.pid
        assert child.ready["data"]["grandchild_pid"] is None


@pytest.mark.subsystem
def test_emits_the_frames_it_was_asked_for_in_order() -> None:
    """Emit one frame per request, in the order requested, and no others

    Order is asserted rather than membership because a lifecycle test reads a
    sequence: "the frames received before the deadline" is a prefix of what the
    script was told to emit, and a prefix is only meaningful if the order is the
    one the caller asked for.

    The sequence compared here starts *after* the readiness frame, which the
    spawn helper has already consumed and checked. Comparing the whole list
    rather than its members is what makes "and no others" part of the claim: a
    script that emitted a third, unasked-for frame fails here.
    """
    with _fixture_child("--emit-frame", "alpha", "--emit-frame", "beta") as child:
        lines = child.drain_stderr(READINESS_TIMEOUT_SECONDS)

    stages = [_decode_frame(line)["stage"] for line in lines]

    assert stages == ["alpha", "beta"]


@pytest.mark.subsystem
def test_the_real_decoder_accepts_every_frame_the_fixture_emits() -> None:
    """Emit frames the shipped parent reads as diagnostics, not as parse errors

    "Emits known frames" means "emits frames the real parent accepts". This case
    is what makes that true, and it is the reason the fixture is allowed to
    hand-write a frame instead of importing the worker's encoder.

    It is asserted by feeding the bytes the script really wrote through
    ``worker_runner._consume_stderr_line`` -- the router production uses. A
    comparison of the fixture's field names against the worker's emitter would be
    weaker in both directions: it would pass on a field set no decoder accepts,
    and it would fail on a harmless reordering.

    The negative half matters as much as the positive one. A frame that fails to
    decode is not raised by the parent, it is *sampled* into ``parse_errors`` and
    the run continues -- so a malformed fixture frame would be silently absent
    from every downstream assertion rather than failing one.
    """
    with _fixture_child("--emit-frame", "alpha") as child:
        # The readiness line is put back at the front. The spawn helper consumes
        # it before yielding, and it is the frame with the most structure to get
        # wrong -- a nested object holding an int pid beside a JSON `null` -- as
        # well as the one a reaping harness has to read. Leaving it out would
        # check the decoder against the fixture's simplest output only.
        lines = [child.ready_line, *child.drain_stderr(READINESS_TIMEOUT_SECONDS)]

    state = _route_through_real_decoder(lines)

    assert [entry["stage"] for entry in state.worker_entries] == [
        READY_STAGE,
        "alpha",
    ]
    assert state.parse_errors == []
    assert state.tail == ""


@pytest.mark.subsystem
def test_writes_the_requested_payload_to_stdout_unmodified() -> None:
    """Hand back exactly the bytes it was told to write, on the payload channel

    Standard output is the channel the parent treats as the rendered page, so a
    byte the script adds or rewrites becomes a byte in the page. Two hazards are
    deliberately built into the payload rather than described in a comment: a
    newline, because text-mode writing rewrites it to a carriage-return pair on
    Windows, and a non-ASCII character, because a Windows console codepage would
    otherwise decide the encoding of something the parent decodes as UTF-8.

    Compared whole rather than by substring, so truncation and a mangled decode
    fail here too.
    """
    with _fixture_child("--stdout", WORKER_STDOUT) as child:
        # Waited for explicitly. Readiness is announced before the payload is
        # written, so leaving the block here would race teardown against the
        # write; the helper turns that mistake into a sentence, but the fix is
        # to wait.
        assert child.wait_for_exit() == 0

    # Compared as bytes, which is the literal claim. Decoding first would turn a
    # payload the script had mangled into a `UnicodeDecodeError` from the test's
    # own comparison rather than into a readable inequality.
    assert child.stdout_bytes() == WORKER_STDOUT.encode("utf-8")


@pytest.mark.subsystem
def test_garbage_mode_produces_each_shape_the_parent_must_survive() -> None:
    """Produce all four malformations at once, and let the real router sort them

    "Garbage on stderr" is not one thing. The parent's line router has three
    distinct outcomes and a decode step in front of them, and a fixture that
    emitted only unparseable JSON would leave the other paths untested while
    looking like it covered them.

    The four shapes and where each must land:

    ============================  =========================
    Shape                         Destination
    ============================  =========================
    an ordinary non-frame line    the stderr tail
    a frame that is not JSON      a parse-error sample
    a frame that is JSON, not an  a parse-error sample
    object
    bytes that are not UTF-8      replaced, then the tail
    ============================  =========================

    The last one is the reason this module reads stderr as bytes throughout. It
    is also the one that would not merely fail but *raise* if the parent had
    decoded strictly, which is a crash rather than a diagnostic -- so the case
    asserts the routing rather than only that nothing blew up.
    """
    with _fixture_child("--stderr-garbage") as child:
        lines = child.drain_stderr(READINESS_TIMEOUT_SECONDS)

    state = _route_through_real_decoder(lines)

    assert state.worker_entries == []
    # Exactly two: one frame that is not JSON at all, one that is JSON but not an
    # object. A count of one would pass on a router that had lost either branch.
    assert len(state.parse_errors) == 2
    assert GARBAGE_PLAIN_LINE in state.tail
    # The undecodable bytes survive as a replacement character rather than as an
    # exception; asserting the marker is what distinguishes "replaced" from
    # "silently dropped".
    assert "�" in state.tail


@pytest.mark.subsystem
def test_hang_mode_outlives_its_own_startup_and_dies_when_killed() -> None:
    """Keep running after announcing itself, and stop when signalled

    The claim a timeout test rests on: the child is still there to be killed when
    the deadline fires. Asserted as a *state* -- the process has not exited --
    and never as an elapsed duration, because a duration assertion would be
    measuring the runner rather than the script.

    The second half is what stops this fixture becoming a suite-wide hazard. A
    process that ignored the kill would leak one child per case, and the leak
    would show up as an exhausted process table in some unrelated job rather than
    as a failure here.
    """
    with _fixture_child("--hang") as child:
        # A liveness bound, not a startup budget. `poll()` alone runs
        # microseconds after the readiness line is read, while the child's
        # interpreter may still be shutting down, so it passes whether or not
        # the child hangs -- measured: with `--hang` turned into a no-op the
        # whole module still passed, five runs out of five. Waiting instead
        # inverts the claim: this can only fail if the child *died*, which is
        # the thing under test, so it is not the fixed startup threshold section
        # 5.2a forbids.
        with pytest.raises(subprocess.TimeoutExpired):
            child.proc.wait(timeout=HANG_LIVENESS_SECONDS)

        _kill_pid(child.proc.pid)
        code = child.wait_for_exit()

    # Not `is not None`, which `Popen.wait` guarantees and so asserts nothing:
    # a killed process reports `-SIGKILL` on POSIX and taskkill's 1 on Windows.
    assert code != 0


@pytest.mark.subsystem
def test_exits_with_the_code_it_was_given() -> None:
    """Report the failure a non-zero exit is meant to stand for

    The parent turns a non-zero exit into a ``RuntimeError`` naming the code, so
    a fixture that could only exit zero could not drive that path at all. The
    code asserted is a specific non-zero value rather than "not zero": an exit
    the script never intended -- an unhandled exception, which leaves 1 -- would
    otherwise satisfy the case while meaning something else entirely.
    """
    with _fixture_child("--exit-code", "3") as child:
        code = child.wait_for_exit()

    assert code == 3


@pytest.mark.subsystem
def test_spawns_a_live_descendant_and_reports_its_pid() -> None:
    """Build a real process tree, and say what is in it

    A parent that kills only its direct child leaves a grandchild orphaned. That
    defect cannot be observed without a grandchild, and the fixture is where one
    comes from -- but a harness can only reap what it can name, and a pid is the
    only safe name. Matching on a process *name* instead would be vulnerable to
    pid reuse and would reach processes this suite never started.

    So two claims, and both are needed before a later step can assert anything
    about orphans: the descendant is a genuinely running process, and the pid in
    the readiness frame is that process rather than a plausible-looking integer.

    This case deliberately stops there. Whether the parent's termination reaches
    the descendant is the lifecycle step's claim, not this one's; asserting it
    here would test the runner through the fixture and blur which of the two a
    failure belonged to.
    """
    with _fixture_child("--spawn-grandchild", "--hang") as child:
        grandchild = child.grandchild_pid

        assert isinstance(grandchild, int)
        assert grandchild != child.proc.pid

        # Observed after a bound, not at once, and the bound is spent on the
        # child's own liveness so nothing here is a bare sleep. Checking
        # immediately is too early to mean anything: a descendant replaced by
        # `python -c pass` is still genuinely running for its first few tens of
        # milliseconds, so the probe answers "alive" truthfully and the case
        # proves nothing. Measured -- that mutation survived an immediate probe
        # and fails this one.
        with pytest.raises(subprocess.TimeoutExpired):
            child.proc.wait(timeout=HANG_LIVENESS_SECONDS)

        assert _pid_is_alive(grandchild), (
            f"the readiness frame reported pid {grandchild}, which is not a "
            f"running process"
        )

    # What "running" costs, and where it still is not free. A signal-0 probe
    # succeeds against a *zombie*, and the descendant is a child of a fixture
    # child that hangs and never reaps it -- so on POSIX the obvious probe
    # cannot tell "running" from "exited but unreaped". `_pid_is_alive` reads
    # `/proc/<pid>/stat` on Linux to close that, which is one `open` and no new
    # dependency. It is not closed on macOS or the BSDs, where there is no
    # `/proc` and the probe falls back to the weaker claim; a process-table
    # library would be the fix, and this suite does not carry one.


@pytest.mark.subsystem
def test_a_child_that_dies_talking_is_reported_with_its_exit_code() -> None:
    """Diagnose a bad command line as one, not as a startup timeout

    The most common failure in this module is not a slow machine, it is a flag
    the script does not have -- because each case is written *before* the flag it
    drives, so it meets ``argparse`` first. ``argparse`` writes usage to stderr
    and then exits 2, which is the awkward shape: a line *does* arrive, so the
    poll's exit-code branch never runs, and without the readiness decode being
    guarded the report would name neither the exit code nor the command.

    This is the case that would have been missing when the behaviour it names
    was added, so it is written against the real thing: an unknown flag.
    """
    with pytest.raises(AssertionError) as excinfo, _fixture_child("--no-such-flag"):
        pass

    message = str(excinfo.value)
    assert "exited with code 2" in message
    assert "--no-such-flag" in message
    assert "usage:" in message


@pytest.mark.subsystem
def test_a_failure_inside_the_block_carries_the_childs_own_output() -> None:
    """Attach what the child said to a failure about what the child did

    Section 5.4 requires child output to be captured and attached on failure.
    The child is a separate process, so pytest shows nothing of it: without this
    every failure here reads "the child did not do what was expected" with no
    way to see what it did instead.

    The failure is forced rather than provoked, because the claim is about the
    reporting path and not about any particular way of reaching it. Asserting
    that the original message survives matters as much as the attachment --
    wrapping an assertion is an easy way to lose the sentence that named the
    problem.
    """
    with (
        pytest.raises(AssertionError) as excinfo,
        _fixture_child("--emit-frame", "alpha", "--hang"),
    ):
        raise AssertionError("the original complaint")

    message = str(excinfo.value)
    assert "the original complaint" in message
    assert "argv:" in message
    # The child's real frame, not a placeholder: this is what distinguishes an
    # attachment that captured the stream from one that captured an empty one.
    assert "alpha" in message
