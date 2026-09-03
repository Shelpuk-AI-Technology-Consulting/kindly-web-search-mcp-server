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
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _pid_is_alive(pid: int) -> bool:
    """Report whether a pid names a running process.

    Args:
        pid: The process to probe.

    Returns:
        ``True`` while the process exists. On POSIX a signal-0 probe also
        succeeds against a zombie, so callers must reap a direct child before
        trusting a ``False`` from this function to mean anything.
    """
    if sys.platform == "win32":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            check=False,
            text=True,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
        stdout_sink: Single-element list receiving the whole stdout payload.
    """

    proc: subprocess.Popen[bytes]
    stderr_lines: queue.Queue[bytes | None]
    argv: list[str] = field(default_factory=list)
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

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            One line, including its terminator.

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

    try:
        # The readiness frame is the script's first output by contract, so this
        # both waits for startup and asserts that ordering in one step.
        first = child.next_stderr_line(READINESS_TIMEOUT_SECONDS)
        child.ready = _decode_frame(first)
        assert child.ready.get("stage") == READY_STAGE, (
            f"the first stderr frame was {child.ready.get('stage')!r}, "
            f"not {READY_STAGE!r}"
        )
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
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def test_the_fixture_child_imports_only_the_standard_library() -> None:
    """Keep the fixture independent of the code it is used to test

    A fixture that imported ``kindly_web_search_mcp_server`` could reproduce a
    production defect rather than expose it: a frame built by the same encoder
    the parent decodes proves the two agree with themselves, not that either is
    right. A third-party import would additionally make the script fail to start
    in any environment without that package, which is a confusing way to learn a
    dependency is missing.

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
        lines = child.drain_stderr(READINESS_TIMEOUT_SECONDS)

    state = _route_through_real_decoder(lines)

    assert [entry["stage"] for entry in state.worker_entries] == ["alpha"]
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

    assert child.stdout_bytes().decode("utf-8") == WORKER_STDOUT


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
        assert child.proc.poll() is None, "the hanging child exited on its own"
        _kill_pid(child.proc.pid)
        code = child.wait_for_exit()

    assert code is not None


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
        assert _pid_is_alive(grandchild), (
            f"the readiness frame reported pid {grandchild}, which is not running"
        )

    # Triage of a mutant this case cannot kill. Replacing the descendant's sleep
    # with `pass` -- so it exits at once -- leaves every assertion above passing.
    # Measured: it survives. The reason is not a gap in the assertions but in
    # what a pid can be asked on POSIX: the descendant is a child of the fixture
    # child, which hangs and never reaps it, so it becomes a zombie, and a
    # signal-0 probe succeeds against a zombie. Telling "running" from "exited
    # but unreaped" portably needs a process-table library this suite does not
    # depend on. The claim that survives is the one asserted: the reported pid
    # names a process that exists and is not the child itself.
