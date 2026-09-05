"""Anti-flake helpers for tests that start a real process or a real socket.

Section 5.4 of ``.system_design/TEST_SUITE.md`` places five obligations on every
such test: a readiness handshake rather than a sleep, ephemeral ports and
isolated profile directories per test, a per-test timeout, cleanup in
``finally`` keyed on **the pids this test spawned**, and the child's own
stdout/stderr captured and attached on failure. This module is where four of
them live; the fifth, isolated directories, is pytest's ``tmp_path_factory``
and needs nothing from here.

**The cleanup invariant is amended by this module, deliberately.** Section 5.4
says "keyed on the PIDs this test spawned". A walked descendant is a pid the
*kernel* reported, not one the test spawned, and reaping only what the test
spawned is exactly the defect production shipped: its tree is worker -> browser
-> renderers, and only the first of those announces anything. The invariant this
module holds is the amended one -- **keyed on a pid this test spawned, plus the
transitive descendants of that pid observed while it was still alive** -- which
is still not a name scan: the walk is rooted at a pid the caller owns and runs
before that process dies, so a recycled pid cannot enter the set through it.

**Nothing here signals a process group.** Production's reaper signals descendant
groups before it signals pids, because a browser leads a group of its own and
one call takes it with every renderer. Correct there, forbidden here: a test
process shares its group with everything it spawns, so the same call written in
a test reaches the pytest session and the shell that started it. The cost is
real and is not hidden -- a descendant spawned *after* the walk is not reached
by this module, and production closes that with the group pass this one refuses.

**Nothing here imports the package under test.** The parent-pid parse and the
descendant walk are duplicated from ``scrape/worker_runner.py`` rather than
imported, because that module's own walk is the subject of a later plan step: a
harness that reaped with it could not be used to measure it, and a defect in it
would disappear into a green teardown. The duplication is the point, and the one
trap in the parse is driven by its own case.

Platform reach, stated rather than implied:

- **Linux** enumerates descendants from ``/proc/<pid>/stat`` and kills each by
  pid.
- **Windows** cannot enumerate from the standard library at all, so
  :func:`descendants_of` returns nothing there and :func:`kill_process_tree`
  reaches the tree through ``taskkill /F /T``, which does its walking inside the
  kill.
- **Every other platform** reaches the root only. That is the same degradation
  production accepts, for the same reason: a ``ps`` subprocess fallback is a
  guess at an output format this suite has no lane to check.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

#: Hard ceiling on waiting for a child to announce itself. Thirty seconds is not
#: a measurement of anything -- it is far longer than a Python interpreter has
#: ever needed to start, chosen so the deadline only ever fires on a child that
#: is genuinely not going to speak. Section 5.2a forbids a startup *budget*
#: assertion for the opposite reason: a millisecond threshold measures a loaded
#: runner and an antivirus scanner's process-start delay.
READINESS_TIMEOUT_SECONDS = 30.0

#: Ceiling on reaping a child during teardown. Shorter than the readiness budget
#: because by this point the process has already been signalled.
REAP_TIMEOUT_SECONDS = 10.0

#: Bounded moment a child that is already exiting is given, before it is decided
#: that it has to be killed.
EXIT_GRACE_SECONDS = 0.5

#: One slice of a liveness poll on POSIX, where a probe is a signal-0 call and a
#: file read.
POLL_SLICE_SECONDS = 0.05

#: One slice of a liveness poll on Windows, where a probe spawns ``tasklist``.
#: Twenty times the POSIX slice because that call costs on the order of a
#: hundred milliseconds: a shorter slice would start processes faster than they
#: finish, and the loop's real cadence would be process creation rather than the
#: bound the caller asked for.
WINDOWS_POLL_SLICE_SECONDS = 1.0


def poll_slice_seconds() -> float:
    """Choose the liveness poll slice for this platform.

    Returns:
        Seconds to wait between liveness probes.
    """
    return (
        WINDOWS_POLL_SLICE_SECONDS if sys.platform == "win32" else POLL_SLICE_SECONDS
    )


def kill_pid(pid: int, *, timeout: float = REAP_TIMEOUT_SECONDS) -> None:
    """Kill one process by pid, tolerating its having already exited.

    Keyed on a pid the caller spawned or walked to, never on a process name: a
    name scan is vulnerable to pid reuse and would reach a developer's own
    processes.

    Args:
        pid: The process to kill.
        timeout: Ceiling on the Windows ``taskkill`` call, which is a subprocess
            and can therefore stall.
    """
    # `SIGKILL` does not exist on Windows and `Popen.kill` is not available for a
    # pid this process does not own as a `Popen`, so the platforms diverge here.
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    # Bounded and swallowed because this runs in `finally`: a `taskkill` stalled
    # behind a scanner must fail the case that is already failing, not hang the
    # suite in teardown.
    except (OSError, subprocess.TimeoutExpired):
        return


def pid_is_alive(pid: int, *, timeout: float = REAP_TIMEOUT_SECONDS) -> bool:
    """Report whether a pid names a running process.

    Args:
        pid: The process to probe.
        timeout: Ceiling on the Windows ``tasklist`` call.

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
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A signal-0 probe succeeds against a zombie, so on Linux the process state
    # is read as well. Cheap -- one `open` -- and it is what lets a descendant
    # assertion mean "running" rather than "exists in the process table".
    # `/proc` is Linux-only; elsewhere the probe stays as strong as it was.
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    # Field three, after the comm field, which is parenthesised and may itself
    # contain spaces -- so split after the last ")" rather than on whitespace.
    _, _, rest = stat.rpartition(")")
    return rest.split()[0] != "Z"


def wait_until_gone(pid: int, *, timeout: float = REAP_TIMEOUT_SECONDS) -> bool:
    """Wait, under a bound, for a pid to stop naming a running process.

    Bounded polling rather than a single probe. A probe taken microseconds after
    a kill cannot tell a live process from a dying one, so a case that asserted
    on one would pass against a reaper that signalled nothing at all on a slow
    enough machine, and fail against a correct one on a fast machine.

    Args:
        pid: The process to wait for.
        timeout: Seconds to wait before giving up.

    Returns:
        ``True`` once the process is gone, ``False`` if it is still there at the
        deadline.
    """
    slice_seconds = poll_slice_seconds()
    deadline = time.monotonic() + timeout
    while True:
        if not pid_is_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(slice_seconds)


def parse_parent_pid(stat_text: str) -> int | None:
    """Read a process's parent pid out of one ``/proc/<pid>/stat`` line.

    The second field of that line is the executable name in brackets, and the
    kernel neither escapes nor rejects spaces or a closing bracket inside it.
    Splitting the line on whitespace therefore reads some other field entirely,
    and against a name containing a space and a digit that is *silent*: it
    yields a plausible small integer, which this module would then treat as a
    process it may kill. Everything after the **last** bracket is read for that
    reason.

    Args:
        stat_text: One line of a ``/proc/<pid>/stat`` file.

    Returns:
        The parent pid, or ``None`` if the line carries no readable one. A
        truncated or empty line is ordinary rather than exceptional here: the
        process may exit between the directory listing and the read.
    """
    _, bracket, rest = stat_text.rpartition(")")
    if not bracket:
        return None
    # After `comm` the fields are `state ppid pgrp ...`, so the parent is the
    # second.
    fields = rest.split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def read_parent_map() -> dict[int, int]:
    """Snapshot every visible process's parent, from ``/proc``.

    Returns:
        Each visible pid mapped to its parent's pid. Empty when ``/proc`` cannot
        be read at all, which is every non-Linux platform -- there the caller
        falls back to a kill that walks the tree for itself, or to the root
        alone.
    """
    parent_map: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return parent_map
    for entry in entries:
        # `isdecimal`, not `isdigit`: the latter is true for characters like
        # 'squared' that `int()` then rejects.
        if not entry.isdecimal():
            continue
        # One unreadable entry costs its own row and nothing else: processes are
        # exiting underneath this loop by definition, and abandoning the scan
        # over one of them could abandon the descendant that matters.
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                stat_text = fh.readline()
        except OSError:
            continue
        parent_pid = parse_parent_pid(stat_text)
        if parent_pid is not None:
            parent_map[int(entry)] = parent_pid
    return parent_map


def collect_descendants(parent_map: Mapping[int, int], root: int) -> list[int]:
    """List every transitive descendant of ``root``.

    Transitive, not just children: production's tree is worker -> browser ->
    renderers, so a walk that stopped at the first generation would reproduce
    the defect one level deeper.

    Args:
        parent_map: Each pid mapped to its parent's pid, from
            :func:`read_parent_map`.
        root: The process whose descendants are wanted. It is never included --
            the caller kills it separately and last.

    Returns:
        The descendants, in no particular order.
    """
    children: dict[int, list[int]] = {}
    for pid, parent_pid in parent_map.items():
        children.setdefault(parent_pid, []).append(pid)

    # `seen` is not an optimisation. The map is assembled from many files that
    # were not read atomically while pids were being recycled, so it can contain
    # a cycle or a self-parent -- and a loop here would hang inside a `finally`
    # during teardown, which is the least diagnosable failure this module could
    # produce.
    descendants: list[int] = []
    seen = {root}
    pending = list(children.get(root, ()))
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        pending.extend(children.get(pid, ()))
    return descendants


def descendants_of(pid: int) -> list[int]:
    """List the running descendants of a process, by parentage.

    Callers get this for assertions -- "the tree is gone" needs the tree named
    -- and :func:`kill_process_tree` uses it internally. It answers only on
    Linux: Windows offers no descendant enumeration in the standard library, so
    there this returns nothing while the reap still reaches the tree through
    ``taskkill /T``, and every other platform reaches the root alone. That
    degradation is stated rather than papered over with a ``ps`` subprocess,
    whose output format this suite has no lane to check.

    Args:
        pid: The process whose descendants are wanted. It must still be alive:
            once it dies its children reparent to init and no longer resolve as
            its descendants.

    Returns:
        The transitive descendants, in no particular order. Empty off Linux.
    """
    return collect_descendants(read_parent_map(), pid)


def build_kill_order(
    descendants: Sequence[int], *, root: int, own_pid: int
) -> list[int]:
    """Order the pids of one tree for killing, with the unkillable ones removed.

    **Descendants before the root**, because killing the root first reparents
    everything below it to pid 1, where the pids already collected name
    processes this caller has no claim on any more.

    **The caller and pid 1 are excluded, and that is live rather than
    defensive.** The parent map admits cycles and recycled pids, so the caller's
    own pid can genuinely appear in a descendant list; and pid 1 appears the
    moment a walked process exits and its children reparent between the read and
    the kill. Either signal from a test's teardown ends the session.

    Args:
        descendants: Pids walked to from ``root``.
        root: The process the caller spawned.
        own_pid: The calling process, which must never be signalled.

    Returns:
        The pids to signal, in the order to signal them.
    """
    excluded = {own_pid, 1}
    order = [pid for pid in descendants if pid not in excluded]
    if root not in excluded:
        order.append(root)
    return order


def kill_process_tree(
    root: int,
    *,
    own_pid: int | None = None,
    parent_map: Mapping[int, int] | None = None,
    kill: Callable[[int], None] = kill_pid,
) -> list[int]:
    """Kill a process and every descendant of it, keyed on pids alone.

    On Windows the enumeration does not exist in the standard library, so the
    tree is reached through ``taskkill /F /T``, which walks it inside the kill.
    Its exit status is deliberately ignored: it returns 128 whenever any member
    of the tree has already exited, which is the ordinary outcome against a tree
    whose members churn, so a non-zero status there does not mean the tree
    survived. A caller that needs to know keys on liveness, through
    :func:`wait_until_gone`.

    Args:
        root: The process the caller spawned. Its descendants are walked from
            here, so this must be called **while it is still alive**: once it
            dies its children reparent and are unattributable.
        own_pid: The calling process, defaulting to this one. A parameter so a
            case can drive the exclusion without being the process at risk.
        parent_map: A snapshot to walk, defaulting to a fresh one. A parameter
            for the same reason.
        kill: Signals one pid; :func:`kill_pid` in production use. Injected so
            the exclusions can be driven with no process in existence -- the
            mutation that loses them, run against a real tree, ends the test
            session and produces no report at all.

    Returns:
        The pids that were signalled, in the order they were signalled.
    """
    resolved_own = os.getpid() if own_pid is None else own_pid

    # Windows has no descendant enumeration in the standard library, so the
    # platform's own tree kill stands in for one. The injected killer is called
    # for the root as well, so a caller observing what gets signalled sees the
    # same pid it would see on Linux -- and `taskkill /T` runs regardless of what
    # that killer does, because the alternative is a Windows-only leak whenever a
    # case injects one. What a Windows caller does *not* get is a list of the
    # descendants; `descendants_of` says so by returning nothing.
    #
    # 🔴 **`/T` first, the injected killer second, and the order is the whole
    # thing.** `/T` walks the tree from the parent id in the process snapshot, so
    # it can only find descendants while the root is still alive; run after a
    # kill of the root it reports "no running instance" and every generation
    # survives. Written the other way round -- which reads more naturally, kill
    # then sweep -- this leaked the whole chain, and a Windows run is the only
    # thing that says so. Measured: two cases failed with "generation pid N
    # outlived the tree kill". The Linux path states the same rule as
    # "descendants before the root"; this is that rule wearing a different hat.
    if sys.platform == "win32" and parent_map is None:
        order = build_kill_order([], root=root, own_pid=resolved_own)
        if order:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(root)],
                    capture_output=True,
                    check=False,
                    timeout=REAP_TIMEOUT_SECONDS,
                )
        for pid in order:
            kill(pid)
        return order

    snapshot = read_parent_map() if parent_map is None else parent_map
    order = build_kill_order(
        collect_descendants(snapshot, root), root=root, own_pid=resolved_own
    )
    for pid in order:
        kill(pid)
    return order


#: Failures that pass through the capture untouched. Not reports about the
#: child: the caller interrupting, the interpreter exiting, and an event loop
#: cancelling a task. ``CancelledError`` has to be named explicitly -- it derives
#: from ``BaseException`` and from nothing else, so the other two do not cover
#: it, and E11 converts this suite to ``asyncio``, after which a cancelled task
#: is an ordinary way for one of these blocks to end.
PASS_THROUGH_EXCEPTIONS = (KeyboardInterrupt, SystemExit, asyncio.CancelledError)

#: Ceiling on one connection attempt while waiting for a port to accept. Short,
#: because the loop's own bound is what governs how long the caller waits; this
#: only stops a single attempt against an unreachable address hanging past it.
CONNECT_ATTEMPT_SECONDS = 0.2


def reserve_ephemeral_port(host: str = "127.0.0.1") -> int:
    """Ask the kernel for a free port and then let go of it.

    For a caller that must know the port **before** the artefact starts --
    a server spawned as a separate process takes it on its command line, and
    cannot be asked afterwards.

    **There is a race and it is not closable from here.** The socket is bound to
    port zero, read, and closed, so anything on the machine may take that port
    between this returning and the caller binding it. Where an artefact can bind
    zero *itself* and report what it got, that is strictly better and should be
    preferred -- ``socketserver.TCPServer`` does exactly that, and the fixture
    server in this suite relies on it. This helper exists for the artefacts that
    cannot.

    Args:
        host: Interface to bind while asking. Loopback by default, because a
            port free on one interface is not free on another.

    Returns:
        A port that was unused at the moment of the call.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Deliberately no `SO_REUSEADDR`. With it, a bind can succeed on a port
        # another socket is still holding down, which is the one answer this must
        # never give. **Unchecked**: the case here proves a live listener's port
        # is never returned, and that holds with or without the option, so the
        # option's absence rests on the documented semantics rather than on a
        # measurement.
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def wait_for_accepting_port(
    host: str,
    port: int,
    *,
    timeout: float,
    proc: subprocess.Popen[Any] | None = None,
) -> None:
    """Wait for a socket to accept a connection, under a bound.

    The second form of readiness handshake section 5.4 allows, for an artefact
    reached over a socket rather than a pipe. A connection that succeeds is the
    handshake: it proves the listener is up, which no sleep can.

    Args:
        host: Address to connect to.
        port: Port to connect to.
        timeout: Seconds to keep trying.
        proc: A process serving that port, if one is being supervised. When it
            exits, waiting is pointless and the failure names the exit code
            instead of a timeout -- the same diagnosis the pipe handshake makes,
            for the same reason: the common failure is a bad command line, not a
            slow machine.

    Raises:
        AssertionError: If nothing accepted before the deadline, or the
            supervised process exited first. The message names the address, so a
            failure does not require reading the case to know what was waited
            for.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(
                f"the process serving {host}:{port} exited with code "
                f"{proc.returncode} before the port accepted a connection"
            )
        try:
            with socket.create_connection(
                (host, port), timeout=CONNECT_ATTEMPT_SECONDS
            ):
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"nothing accepted a connection on {host}:{port} within "
                f"{timeout}s"
            )
        time.sleep(POLL_SLICE_SECONDS)


def _pump_lines(stream: IO[bytes], sink: queue.Queue[bytes | None]) -> None:
    """Move complete lines off a pipe onto a queue until the pipe closes.

    A reader thread rather than a blocking read in the caller: a pipe read has
    no timeout, so an artefact that never speaks would hang the suite instead of
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


def _pump_all(stream: IO[bytes], sink: list[bytes]) -> None:
    """Drain a pipe to end of file into a single-element list.

    Drained on a thread even when a caller ignores the result, because a child
    that fills its stdout pipe while nobody reads blocks forever.

    Args:
        stream: The child's standard output, opened in binary mode.
        sink: List the whole payload is appended to once the stream closes.
    """
    sink.append(stream.read())


@dataclass
class CapturedChild:
    """A spawned child and the machinery draining its pipes.

    Attributes:
        proc: The process handle.
        argv: The full command the child was spawned with, quoted in failures.
        stderr_lines: Queue of complete stderr lines, ``None`` marking the end.
        stdout_sink: Single-element list receiving the whole stdout payload.
        ready_line: The line the readiness marker was found on.
        preamble: Any stderr lines that arrived before it.
        deadline_fired: Whether the per-child deadline expired and killed the
            tree. A recorded fact rather than a raised error, because the case
            that asks for a deadline is usually asserting that it fired.
        killed_while_running: Whether teardown had to kill a child that had not
            finished, which makes its stdout payload untrustworthy.
        reap_lock: Serialises every call that could reap the child against the
            deadline watchdog's guard-then-kill. See :meth:`poll_under_lock`.
        seen_stderr: Every line taken off the queue so far, by any reader, in
            order. The queue is destructive, so without this a line consumed to
            synchronise on -- or by an earlier failure report -- would be missing
            from every report afterwards, and the child would read as silent.
    """

    proc: subprocess.Popen[bytes]
    argv: list[str]
    stderr_lines: queue.Queue[bytes | None]
    stdout_sink: list[bytes] = field(default_factory=list)
    ready_line: bytes = b""
    preamble: list[bytes] = field(default_factory=list)
    deadline_fired: bool = False
    killed_while_running: bool = False
    reap_lock: threading.Lock = field(default_factory=threading.Lock)
    seen_stderr: list[bytes] = field(default_factory=list)

    def poll_under_lock(self) -> int | None:
        """Reap the child if it has finished, without racing the watchdog.

        **Every reaping call in this module goes through here**, and the reason
        is a window that was measured rather than reasoned about. The watchdog
        guards itself with a ``poll()`` before it signals -- the guard CPython
        puts inside ``Popen.send_signal`` -- but unlike CPython's, this one is
        separated from the kill by a whole ``/proc`` walk, which takes about
        8 ms and up to 14 ms on an idle machine. A caller reaping inside that
        window frees the pid, and the signal that follows lands on whoever
        inherited it. Measured on the unmodified code: **13 of 120 runs** issued
        the deadline's kill against a pid that had already been reaped.

        The lock closes it by making the watchdog's guard-and-kill atomic
        against every reap this module performs. It is held only for a
        ``poll()``, never across a blocking wait -- a lock held for the duration
        of a wait would block the watchdog for exactly the interval the deadline
        is meant to fire in.

        A caller that reaches past this object for ``child.proc.wait()`` is
        outside the lock and back in the window; nothing in this module does.

        Returns:
            The exit code, or ``None`` while the child is still running.
        """
        with self.reap_lock:
            return self.proc.poll()

    def stdout_bytes(self) -> bytes:
        """Give the child's whole standard output, once the stream has closed.

        Returns:
            Every byte the child wrote to standard output.

        Raises:
            AssertionError: If the stream never closed, or if teardown had to
                kill a child that was still running. The second is the
                interesting one: readiness is announced *before* a payload is
                written, so a caller that leaves the block as soon as the child
                is ready races teardown against the write and compares a
                truncated payload. That turns forgetting into a sentence rather
                than into a flake.
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

        Polls rather than calling ``Popen.wait``, which is not a preference:
        ``wait`` reaps, and reaping has to happen under the lock that the
        deadline watchdog also takes. See :meth:`poll_under_lock`. The cost is
        one poll slice of latency on a child that has already exited.

        Args:
            timeout: Seconds to wait.

        Returns:
            The child's exit code.

        Raises:
            AssertionError: If the child is still running at the deadline.
        """
        deadline = time.monotonic() + timeout
        while True:
            code = self.poll_under_lock()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the child was still running after {timeout}s when it was "
                    f"expected to exit.{self.captured_report()}"
                )
            time.sleep(POLL_SLICE_SECONDS)

    def next_stderr_line(self, timeout: float) -> bytes:
        """Take the next complete stderr line, waiting up to ``timeout``.

        Waits in slices rather than in one long block so that a child which has
        *died* is reported as dead within a slice instead of after the whole
        deadline. The two failures need different fixes and a shared message
        would send the next reader the wrong way: a child slow to start is a
        runner problem, a child that exited is a bad command line -- and the
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
                    f"the child wrote no stderr line within {timeout}s and is "
                    f"still running.{self.captured_report()}"
                )
            try:
                line = self.stderr_lines.get(timeout=min(0.1, remaining))
            except queue.Empty:
                # An exited child will never produce the awaited line, so there
                # is nothing to gain by waiting out the rest of the deadline.
                if self.poll_under_lock() is not None:
                    raise AssertionError(
                        f"the child exited with code {self.proc.returncode} "
                        f"before writing the expected stderr line."
                        f"{self.captured_report()}"
                    ) from None
                continue
            if line is None:
                raise AssertionError(
                    "the child's stderr closed before the expected line "
                    f"(exit code {self.poll_under_lock()}).{self.captured_report()}"
                )
            self.seen_stderr.append(line)
            return line

    def captured_report(self) -> str:
        """Render whatever the child has said so far, for a failure message.

        Section 5.4 requires child output to be captured and attached on
        failure. Without it every failure reads as "the child did not do what
        was expected" with no way to see what it did instead, and the child is a
        separate process whose output pytest never shows.

        Returns:
            A block naming the argv and quoting the captured streams, ready to
            append to a failure.
        """
        # Non-blocking: this runs on a failure path and must not add a second
        # wait to a case that is already failing. Whatever it takes off the queue
        # joins everything already read, because the queue is destructive: a line
        # consumed by the readiness wait, by a caller synchronising on it, or by
        # an earlier report would otherwise be absent from this one.
        while True:
            try:
                line = self.stderr_lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            self.seen_stderr.append(line)
        stdout = self.stdout_sink[0] if self.stdout_sink else b"<still open>"
        return (
            f"\n  argv:   {self.argv}"
            f"\n  stderr: {b''.join(self.seen_stderr)!r}"
            f"\n  stdout: {stdout!r}"
        )


def _await_marker(child: CapturedChild, marker: bytes, timeout: float) -> None:
    """Read stderr until the readiness marker arrives, or the bound expires.

    Lines before the marker are kept rather than discarded: an artefact is
    entitled to log before it announces itself, and what it said is the first
    thing a reader wants when the announcement never comes.

    Wrapped by the caller rather than raising its own composite message, because
    the interesting failure is not the timeout, it is the *chatty death*: a
    child given an unknown flag has ``argparse`` write usage to stderr and then
    exit 2, so lines arrive, the exit-code branch inside
    :meth:`CapturedChild.next_stderr_line` is never reached, and the deadline
    fires against a process that has been dead for thirty seconds.

    Args:
        child: The running child.
        marker: Substring the announcing line must contain.
        timeout: Seconds to wait for it.

    Raises:
        AssertionError: If the marker did not arrive in time. The message names
            the marker and carries the capture.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"the child never wrote a line containing {marker!r} within "
                f"{timeout}s.{child.captured_report()}"
            )
        line = child.next_stderr_line(remaining)
        if marker in line:
            child.ready_line = line
            return
        child.preamble.append(line)


@contextlib.contextmanager
def spawned_child(
    argv: Sequence[str],
    *,
    readiness_marker: bytes,
    env: Mapping[str, str] | None = None,
    readiness_timeout: float = READINESS_TIMEOUT_SECONDS,
    deadline: float | None = None,
    kill: Callable[[int], None] = kill_pid,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
    on_spawn: Callable[[int], None] | None = None,
) -> Iterator[CapturedChild]:
    """Spawn a child, hand-shake with it, and reap its whole tree afterwards.

    Every obligation section 5.4 places on a test that starts a process, in one
    context manager.

    **Both pipes are drained, on threads, always.** Not tidiness: a child that
    writes its payload before it stops making progress blocks inside that write
    as soon as the payload passes the pipe capacity -- 64 KiB on Linux, and an
    unspecified, smaller figure on Windows -- if the caller drains only stderr.
    Every later assertion about a child that is "still running" then passes for
    the wrong reason, because a child blocked in ``write`` *is* still running.

    **The deadline is a watchdog thread, and what it buys is narrow.** Every
    other bound here is a caller-side wait, which cannot help when the block's
    own body is what blocks. The thread can, and it brings the pid-reuse hazard
    with it: once a child is reaped its pid is free, immediately on Windows and
    quickly on Linux under a container's low ``pid_max``. What closes that is a
    ``poll()`` taken **under a lock the kill is taken under too** -- see
    :meth:`CapturedChild.poll_under_lock`, which records what the window
    measured. The poll on its own is not enough, and the claim that it was is one
    a probe falsified: CPython's ``Popen.send_signal`` polls and signals with
    nothing in between, while this one has a ``/proc`` walk between them.
    Cancelling the timer in ``finally`` is thread hygiene and determinism,
    **not** the safety mechanism.

    **The reap walks while the child is alive.** A child that has already exited
    took its walkability with it: its descendants reparent to init and no longer
    resolve as its descendants. That is why every caller building a chain also
    asks the child to hang.

    Args:
        argv: The command to spawn.
        readiness_marker: Substring of the line the artefact announces itself
            with. A parameter rather than a constant, so this module does not
            know any one artefact's protocol.
        env: Environment for the child, defaulting to a copy of this process's.
            Inherited rather than emptied because Windows needs ``SYSTEMROOT``
            to start a process at all and these helpers are portable.
        readiness_timeout: Ceiling on the handshake.
        deadline: Seconds **after the handshake completes** at which the child
            and its tree are killed, or ``None`` for no watchdog. Measured from
            readiness rather than from the spawn, which is what makes it a bound
            on the caller's block rather than on the child's whole life: timed
            from the spawn, it would have to be longer than the readiness budget
            or a slow start would be killed mid-handshake and reported as a
            deadline -- the wrong diagnosis, and a constraint every caller would
            then have to reason about. Nothing is lost, because a handshake that
            never completes is already bounded by ``readiness_timeout`` and
            reaped by the same ``finally``.
        kill: Signals one pid; injected so a case can observe what was signalled
            without a process being at risk.
        thread_factory: Builds the pipe pumps; injected so a case can make
            thread creation fail, which is the only way to reach the window
            between spawning and wiring up.
        on_spawn: Called with the child's pid the moment it exists, for a case
            that must reason about a child the context manager never yielded.

    Yields:
        The running child, with its readiness line already read.

    """
    started = time.monotonic()
    command = list(argv)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ if env is None else env),
    )
    if on_spawn is not None:
        on_spawn(proc.pid)
    child = CapturedChild(proc=proc, argv=command, stderr_lines=queue.Queue())

    def fire_deadline() -> None:
        """Kill the child's tree, unless it has already been reaped."""
        # Guard and kill under one lock, because they are not one operation: a
        # `/proc` walk sits between them. Taken elsewhere only around a `poll()`,
        # so nothing in this module can reap the child -- and free its pid --
        # between the check and the signal. `CapturedChild.poll_under_lock`
        # records what that window measured.
        with child.reap_lock:
            if proc.poll() is not None:
                return
            child.deadline_fired = True
            kill_process_tree(proc.pid, kill=kill)

    # The `try` opens the moment the process exists, not once it is fully set
    # up. Everything between those two points can still fail -- `Thread.start`
    # raises under thread exhaustion -- and a failure there leaves a running
    # child with nothing to reap it but its own five-minute backstop.
    threads: list[threading.Thread] = []
    running_pumps: list[threading.Thread] = []
    timer: threading.Timer | None = None
    try:
        assert proc.stdout is not None and proc.stderr is not None
        threads = [
            thread_factory(
                target=_pump_lines, args=(proc.stderr, child.stderr_lines), daemon=True
            ),
            thread_factory(
                target=_pump_all, args=(proc.stdout, child.stdout_sink), daemon=True
            ),
        ]
        # Recorded one at a time. If the *second* `start()` raises -- which is
        # the thread-exhaustion case this whole ordering exists for -- joining an
        # unstarted thread below raises `RuntimeError` from inside `finally` and
        # replaces the failure that actually happened.
        for thread in threads:
            thread.start()
            running_pumps.append(thread)

        try:
            _await_marker(child, readiness_marker, readiness_timeout)
        except AssertionError as failure:
            # A child that has already exited did not merely fail to announce
            # itself -- it died talking, which is a different diagnosis. Waited
            # for briefly first: `argparse` writes its usage and *then* exits,
            # so polling the instant the lines arrive would often report a
            # still-running child and lose the exit code that names the cause.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=EXIT_GRACE_SECONDS)
            code = proc.poll()
            died = "" if code is None else f" The child exited with code {code}."
            raise AssertionError(f"{failure}{died}") from failure
        print(
            f"child readiness: {int((time.monotonic() - started) * 1000)} ms "
            f"(telemetry, not asserted)"
        )
        # Armed only now, so the interval it bounds is the caller's block.
        if deadline is not None:
            timer = threading.Timer(deadline, fire_deadline)
            timer.daemon = True
            timer.start()
        try:
            yield child
        except PASS_THROUGH_EXCEPTIONS:
            # Never annotated and never swallowed: these are the caller, the
            # interpreter or an event loop giving up, not the child
            # misbehaving, and a paragraph of captured stderr stapled to one is
            # noise attached to a signal about something else.
            raise
        except BaseException as failure:
            # Section 5.4 requires child output on failure. Added as a *note*
            # rather than re-raised inside a new exception: an arbitrary
            # exception cannot be rebuilt with a longer message, so wrapping
            # would either lose the class or work for `AssertionError` alone --
            # and `pytest.fail` raises `Failed`, which is not one.
            failure.add_note(child.captured_report())
            raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=REAP_TIMEOUT_SECONDS)
        # A child on its way out is given a bounded moment first. Its stderr
        # reaching end of file does not mean it has exited, so a caller that
        # drained the stream can arrive here microseconds early and record a
        # kill that did not need to happen.
        # Below the timer's join, so the watchdog is provably gone and these
        # calls need no lock -- which is why they may block where
        # `CapturedChild.wait_for_exit` may not.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=EXIT_GRACE_SECONDS)
        # Only reap a child that is still running. One that has already exited
        # is a zombie until waited for, so its pid is still signallable -- and a
        # caller asserting the exit code it chose should not have to reason
        # about whether teardown replaced it.
        if proc.poll() is None:
            child.killed_while_running = True
            kill_process_tree(proc.pid, kill=kill)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=REAP_TIMEOUT_SECONDS)
        for thread in running_pumps:
            thread.join(timeout=REAP_TIMEOUT_SECONDS)
        # Only close a pipe whose pump has finished with it. Reachable when the
        # reap above timed out and a pump is still inside `read`; closing under
        # it there would raise in a thread nothing is watching.
        if all(not thread.is_alive() for thread in running_pumps):
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
