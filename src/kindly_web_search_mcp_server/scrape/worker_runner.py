"""Parent-side management of the headless-browser worker child process.

This module owns everything the parent does *to* and *with* the worker
subprocess: spawning it, streaming and framing its standard output and standard
error, heartbeating it while it runs, enforcing its total timeout budget, and
killing its process tree when it overruns or the caller is cancelled. It builds
no *worker* command — :func:`_run_worker_command` executes whatever argv it is
handed, and
:func:`kindly_web_search_mcp_server.scrape.universal_html._build_worker_command`
remains the only place a worker command line is constructed. The one exception
is deliberate and carries no caller data: :func:`_run_pipe_probe` composes a
fixed four-element argv around a literal payload, to measure whether pipes
behave on this host before a real worker is blamed for their not doing so.

**Why this is a module and not a section of another file.** Section 10.4 of
``.system_design/TEST_SUITE.md`` classifies every production file as
hermetically testable or not, and coverage.py's ``omit`` works at file
granularity. The code here has no hermetic seam: proving that a process tree
died, or that a frame split across two pipe reads is reassembled, needs a real
child. The Markdown-suffix probe path it used to share a file with has fifteen
hermetic tests and must stay inside the gating scope. One file could be
classified only one way, and both answers were wrong. Splitting them makes the
classification a property of the module boundary rather than a side-table
somebody has to maintain — which is why ``.coveragerc-gate`` named this file in
its ``omit`` list before the file existed.

**Never write ``from asyncio import create_subprocess_exec`` here.** The
attribute lookup ``asyncio.create_subprocess_exec``, performed at call time
against the shared :mod:`asyncio` module object, is what lets a test replace the
spawn primitive process-wide. A from-import binds the callable at import time
instead, which silently unhooks any such double and spawns real processes from a
lane that must not start one. The characterization tests that relied on exactly
that mechanism have since been moved onto the
:func:`_run_worker_command` seam, so the trap no longer has a live victim in this
repository — the import is still wrong, and is documented here because the
symptom it produces (assertion failures in tests that name neither this module
nor the import) points nowhere near its cause.

The helpers below arrived here verbatim from ``universal_html.py``, because an
extraction that also rewrites what it moves cannot be reviewed as an extraction.
They have since been documented and annotated — the step that annotated the
seam took the docstrings with it, since it was editing the same signatures and
no other step owned the gap.

**The process this module spawns is typed** :class:`~kindly_web_search_mcp_server\
.scrape.types.WorkerProcess`, **and that annotation is a check, not a label.**
mypy compares the real :class:`asyncio.subprocess.Process` against the Protocol
at the spawn site, and every read below against the Protocol's seven members —
so reading an eighth fails, and so does dropping one of the seven from the
Protocol. Before it, the only thing tying the test double's shape to what this
module reads was a hand-written literal in the test suite.

Widening :func:`_emit_worker_heartbeat` and :func:`_terminate_process_tree` to
that Protocol is **not** the hermetic seam the paragraph above refuses. That
refusal is about a spawn-injection parameter, which would let a test replace the
child and contradict the classification this module's existence expresses. An
annotation is erased at run time and always could have been handed a fake;
nothing about the process boundary moved. The rule that follows from it:
structural and contract claims about these two helpers may use a double,
behavioural claims about process termination stay ``subsystem``, because this
module is outside the coverage gate and a hermetic test here earns nothing while
blurring the classification.

**Two Windows guards, deliberately spelled differently.** The procedure, so a
new branch does not have to re-derive it:

* A branch touching stdlib that exists on **one platform only** must be
  ``sys.platform``-guarded, on whichever side that is — mypy narrows on
  ``sys.platform`` and never on ``os.name``, so ``os.name`` leaves the
  platform-exclusive lookup as an ``attr-defined`` error. The run that reads
  that branch is then the native one for a POSIX-only body, and the
  ``--platform win32`` one for a Windows-only body; both exist.
* Otherwise use ``os.name``, which leaves the branch checked on **both** runs.
  Converting :func:`_terminate_process_tree` "for consistency" would make mypy
  treat its whole ``taskkill`` path unreachable on Linux and stop checking it —
  measured with an injected error, reported under ``os.name`` and not under
  ``sys.platform``.

``getattr``/``hasattr`` is a third spelling already used below. It is for
optional *attributes* on a module that does exist, and it is not a substitute
for either guard: it degrades the value to ``Any`` rather than proving anything.

Both spellings are pinned by ``tests/test_worker_runner.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from ..utils.diagnostics import (
    MAX_STDERR_CHARS,
    Diagnostics,
    truncate_text,
)

# Relative, matching this module's other intra-package import above. It is
# NOT what lets the seam's mutation harness check a copied Protocol -- that
# is the copy-anchored `mypy_path`; measured, an absolute import resolves to
# the copy too.
from .types import WorkerProcess


@dataclass
class _StdoutAccumulator:
    """Mutable state for one draining of a child's standard output

    Passed to :func:`_read_stdout_stream`, which owns it for the life of a run
    and mutates it in place, so the caller can read the partial result after a
    timeout cancels the reader.

    Attributes:
        buffer: Raw bytes received so far, decoded only once the child exits.
        bytes_read: Total bytes seen, which is *not* ``len(buffer)`` for the
            stderr twin and is kept symmetrical here.
        last_emit_time: Monotonic time of the last progress record, or of the
            call that seeded the clock; ``0.0`` before either.
        last_emit_bytes: ``bytes_read`` at that record, used to rate-limit
            progress by volume as well as by elapsed time.
    """

    buffer: bytearray = field(default_factory=bytearray)
    bytes_read: int = 0
    last_emit_time: float = 0.0
    last_emit_bytes: int = 0


@dataclass
class _StderrAccumulator:
    """Mutable state for one draining of a child's standard error

    Standard error carries two interleaved things: ``KINDLY_DIAG`` frames the
    worker emits deliberately, and whatever else it or its browser writes. They
    are separated as lines arrive rather than afterwards, so a run that times out
    still yields the frames received before the deadline.

    Attributes:
        buffer: Text received but not yet split into complete lines.
        tail: The most recent non-frame output, capped at the caller's limit.
        bytes_read: Total bytes seen, before decoding.
        last_emit_time: Monotonic time of the last progress record, or of the
            call that seeded the clock.
        last_emit_bytes: ``bytes_read`` at that record.
        worker_entries: Decoded ``KINDLY_DIAG`` frames, merged into the caller's
            diagnostics once the run finishes.
        parse_errors: Up to three samples of frames that would not decode, kept
            so a malformed worker is diagnosable without unbounded logging.
    """

    buffer: str = ""
    tail: str = ""
    bytes_read: int = 0
    last_emit_time: float = 0.0
    last_emit_bytes: int = 0
    worker_entries: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


STREAM_READ_CHUNK = 16_384
STREAM_PROGRESS_INTERVAL_SECONDS = 2.0
STREAM_PROGRESS_MIN_BYTES = 64 * 1024
STREAM_HEARTBEAT_INTERVAL_SECONDS = 2.0
PIPE_PROBE_TIMEOUT_SECONDS = 3.0
PIPE_PROBE_OUTPUT_BYTES = 4 * 1024
PIPE_PROBE_SAMPLE_LIMIT = 400


def _subprocess_launch_options() -> dict[str, Any]:
    """Build the platform-specific keyword arguments for spawning a child

    On Windows a console application spawned from a GUI-less parent flashes a
    console window and joins the parent's process group, so a Ctrl-C intended
    for the parent reaches the child as well. Both are suppressed here.

    Returns:
        Keyword arguments for :func:`asyncio.create_subprocess_exec`. Empty on
        every non-Windows platform, where none of this applies.
    """
    # `sys.platform`, not `os.name`: mypy narrows on the former only, and
    # `subprocess.STARTUPINFO` below is win32-only in typeshed.
    if sys.platform != "win32":
        return {}
    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startupinfo = subprocess.STARTUPINFO()
    if hasattr(subprocess, "STARTF_USESHOWWINDOW"):
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {"creationflags": creationflags, "startupinfo": startupinfo}


def _append_tail_text(existing: str, addition: str, *, limit: int) -> str:
    """Append to a bounded tail, discarding from the front when it overflows

    Keeps the *most recent* text rather than the first, because the end of a
    failing child's stderr is what names the failure.

    Args:
        existing: The tail so far.
        addition: Text to append. An empty string is returned unchanged rather
            than triggering a needless copy.
        limit: Maximum characters to retain.

    Returns:
        The combined text, truncated from the left to ``limit`` characters.
    """
    if not addition:
        return existing
    combined = existing + addition
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


def _consume_stderr_line(
    state: _StderrAccumulator, line: str, *, tail_limit: int
) -> None:
    """Route one complete line of a child's stderr into ``state``

    A line prefixed ``KINDLY_DIAG `` is a structured frame the worker emitted on
    purpose; anything else is ordinary output and joins the tail. A frame that
    will not decode, or that decodes to something other than an object, is
    sampled rather than raised: a malformed frame must not cost the caller the
    run, and must not let a chatty child flood memory either, so at most three
    samples are kept.

    Args:
        state: Accumulator mutated in place.
        line: One line, already stripped of its terminator.
        tail_limit: Maximum characters to retain in ``state.tail``.
    """
    if line == "":
        return
    if line.startswith("KINDLY_DIAG "):
        payload = line[len("KINDLY_DIAG ") :].strip()
        try:
            parsed = json.loads(payload)
        except Exception:
            if len(state.parse_errors) < 3:
                sample, _, _ = truncate_text(payload, 200)
                state.parse_errors.append(sample)
            return
        if isinstance(parsed, dict):
            state.worker_entries.append(parsed)
        else:
            if len(state.parse_errors) < 3:
                sample, _, _ = truncate_text(payload, 200)
                state.parse_errors.append(sample)
        return
    state.tail = _append_tail_text(state.tail, line + "\n", limit=tail_limit)


def _finalize_stderr_state(state: _StderrAccumulator, *, tail_limit: int) -> None:
    """Consume a trailing partial line left after the stream closed

    A child that exits without a final newline leaves its last line in the
    buffer, where the reader's newline loop never sees it. That line is often
    the most interesting one, so it is flushed through the same routing.

    Args:
        state: Accumulator mutated in place.
        tail_limit: Maximum characters to retain in ``state.tail``.
    """
    if not state.buffer:
        return
    line = state.buffer.rstrip("\r")
    state.buffer = ""
    _consume_stderr_line(state, line, tail_limit=tail_limit)


def _maybe_emit_stream_progress(
    diagnostics: Diagnostics | None,
    *,
    stream: str,
    bytes_read: int,
    started: float,
    last_emit_time: float,
    last_emit_bytes: int,
) -> tuple[float, int]:
    """Emit a stream-progress record, but only if enough has changed

    A record per read chunk would bury the diagnostics stream on a large page,
    so one is emitted only once the interval **or** the byte threshold is
    crossed. Either, not both: a slow trickle must still show progress, and a
    fast flood must not wait out the clock to report it.

    The clock is seeded on first use rather than at construction, so a stream
    that stays silent for a minute before its first byte does not emit a record
    the instant it wakes.

    Args:
        diagnostics: Sink, or ``None`` to do nothing.
        stream: ``"stdout"`` or ``"stderr"``, recorded on the entry.
        bytes_read: Total bytes drained from this stream so far.
        started: Monotonic time the run began, for the elapsed figure.
        last_emit_time: Monotonic time of the previous record.
        last_emit_bytes: ``bytes_read`` at the previous record.

    Returns:
        The time and byte count to carry forward, which the caller assigns back
        unconditionally. Unchanged when nothing was emitted, except on the very
        first call, which emits nothing but seeds the clock.
    """
    if diagnostics is None:
        return last_emit_time, last_emit_bytes
    now = time.monotonic()
    if last_emit_time == 0.0:
        last_emit_time = now
    if (now - last_emit_time) < STREAM_PROGRESS_INTERVAL_SECONDS and (
        bytes_read - last_emit_bytes
    ) < STREAM_PROGRESS_MIN_BYTES:
        return last_emit_time, last_emit_bytes
    diagnostics.emit(
        "worker.stream",
        "Streaming worker output",
        {
            "stream": stream,
            "bytes_read": bytes_read,
            "elapsed_ms": int((now - started) * 1000),
        },
    )
    return now, bytes_read


async def _read_probe_stream(
    stream: asyncio.StreamReader | None,
    *,
    byte_limit: int,
) -> tuple[bytes, int, float | None]:
    """Drain a probe stream to EOF, keeping only its first ``byte_limit`` bytes

    Reading continues past the limit rather than stopping at it: the point is to
    measure how much the pipe delivered and how quickly, and abandoning the read
    early would block the child on a full pipe and confound the measurement.

    Args:
        stream: The stream, or ``None`` when the child was spawned without one.
        byte_limit: Maximum bytes to retain for the sample.

    Returns:
        The retained sample, the total number of bytes read, and the monotonic
        time the first byte arrived — ``None`` if nothing ever did.
    """
    if stream is None:
        return b"", 0, None
    buffer = bytearray()
    bytes_read = 0
    first_byte_at: float | None = None
    while True:
        chunk = await stream.read(STREAM_READ_CHUNK)
        if not chunk:
            break
        if first_byte_at is None:
            first_byte_at = time.monotonic()
        bytes_read += len(chunk)
        if len(buffer) < byte_limit:
            remaining = byte_limit - len(buffer)
            buffer.extend(chunk[:remaining])
    return bytes(buffer), bytes_read, first_byte_at


async def _run_pipe_probe(
    *,
    executable: str,
    env: dict[str, str],
    diagnostics: Diagnostics,
) -> None:
    """Measure whether pipes work on this host, before blaming a real worker

    Spawns a short-lived interpreter that writes a known payload to both
    streams, then records how much came back and how fast. When a worker hangs,
    this distinguishes "the browser never started" from "this host's pipes do
    not deliver", which are diagnosed in completely different places.

    It reports rather than raises: a failed probe is a diagnostic finding about
    the host, not a reason to fail the caller's fetch. Every exit path emits
    exactly one record, and a probe that overruns has its process tree killed
    before this returns.

    Args:
        executable: Interpreter to run. The only argv this module composes, and
            it carries no caller data — a fixed four elements around a literal.
        env: Environment for the probe child.
        diagnostics: Sink for the result. Required, not optional: a probe whose
            findings go nowhere has no reason to run.
    """
    probe_payload = (
        "import sys; "
        f"data='x'*{PIPE_PROBE_OUTPUT_BYTES}; "
        "sys.stdout.write(data); sys.stdout.flush(); "
        "sys.stderr.write('probe stderr\\n'); sys.stderr.flush()"
    )
    cmd = [executable, "-u", "-c", probe_payload]
    diagnostics.emit(
        "worker.pipe_probe_started",
        "Initiating pipe probe",
        {
            "timeout_seconds": PIPE_PROBE_TIMEOUT_SECONDS,
            "output_bytes": PIPE_PROBE_OUTPUT_BYTES,
            "executable": executable,
        },
    )
    loop = asyncio.get_running_loop()
    policy = asyncio.get_event_loop_policy()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **_subprocess_launch_options(),
    )
    probe_started = time.monotonic()
    stdout_task = asyncio.create_task(
        _read_probe_stream(proc.stdout, byte_limit=PIPE_PROBE_OUTPUT_BYTES)
    )
    stderr_task = asyncio.create_task(
        _read_probe_stream(proc.stderr, byte_limit=PIPE_PROBE_OUTPUT_BYTES)
    )
    wait_task = asyncio.create_task(proc.wait())
    killed = False
    try:
        await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, wait_task),
            timeout=PIPE_PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        for task in (stdout_task, stderr_task, wait_task):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(stdout_task, stderr_task, wait_task)
        await _terminate_process_tree(proc)
        killed = True
        diagnostics.emit(
            "worker.pipe_probe_error",
            "Pipe probe timed out",
            {
                "error": type(exc).__name__,
                "detail": str(exc),
                "killed": killed,
                "event_loop": loop.__class__.__name__,
                "event_loop_policy": policy.__class__.__name__,
                "elapsed_ms": int((time.monotonic() - probe_started) * 1000),
            },
        )
        return
    except Exception as exc:
        for task in (stdout_task, stderr_task, wait_task):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(stdout_task, stderr_task, wait_task)
        await _terminate_process_tree(proc)
        killed = True
        diagnostics.emit(
            "worker.pipe_probe_error",
            "Pipe probe failed",
            {
                "error": type(exc).__name__,
                "detail": str(exc),
                "killed": killed,
                "event_loop": loop.__class__.__name__,
                "event_loop_policy": policy.__class__.__name__,
                "elapsed_ms": int((time.monotonic() - probe_started) * 1000),
            },
        )
        return

    stdout_bytes, stdout_len, stdout_first = stdout_task.result()
    stderr_bytes, stderr_len, stderr_first = stderr_task.result()
    stdout_sample, stdout_truncated, stdout_sample_len = truncate_text(
        stdout_bytes.decode("utf-8", errors="replace"), PIPE_PROBE_SAMPLE_LIMIT
    )
    stderr_sample, stderr_truncated, stderr_sample_len = truncate_text(
        stderr_bytes.decode("utf-8", errors="replace"), PIPE_PROBE_SAMPLE_LIMIT
    )
    diagnostics.emit(
        "worker.pipe_probe",
        "Pipe probe completed",
        {
            "stdout_len": stdout_len,
            "stderr_len": stderr_len,
            "stdout_sample": stdout_sample,
            "stderr_sample": stderr_sample,
            "stdout_sample_len": stdout_sample_len,
            "stderr_sample_len": stderr_sample_len,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "exit_code": proc.returncode,
            "time_to_first_stdout_ms": (
                None
                if stdout_first is None
                else int((stdout_first - probe_started) * 1000)
            ),
            "time_to_first_stderr_ms": (
                None
                if stderr_first is None
                else int((stderr_first - probe_started) * 1000)
            ),
            "elapsed_ms": int((time.monotonic() - probe_started) * 1000),
            "event_loop": loop.__class__.__name__,
            "event_loop_policy": policy.__class__.__name__,
        },
    )


async def _read_stdout_stream(
    stream: asyncio.StreamReader | None,
    state: _StdoutAccumulator,
    *,
    diagnostics: Diagnostics | None,
    started: float,
) -> None:
    """Drain a child's standard output into ``state`` until it closes

    Bytes are accumulated undecoded and decoded once at the end, because a
    multi-byte character split across two reads would otherwise be corrupted at
    the seam.

    Args:
        stream: The stream, or ``None`` when no pipe was requested.
        state: Accumulator mutated in place, so a cancelled read still leaves
            the caller whatever arrived.
        diagnostics: Sink for progress records, or ``None``.
        started: Monotonic time the run began, for elapsed figures.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(STREAM_READ_CHUNK)
        if not chunk:
            break
        state.buffer.extend(chunk)
        state.bytes_read += len(chunk)
        state.last_emit_time, state.last_emit_bytes = _maybe_emit_stream_progress(
            diagnostics,
            stream="stdout",
            bytes_read=state.bytes_read,
            started=started,
            last_emit_time=state.last_emit_time,
            last_emit_bytes=state.last_emit_bytes,
        )


async def _read_stderr_stream(
    stream: asyncio.StreamReader | None,
    state: _StderrAccumulator,
    *,
    diagnostics: Diagnostics | None,
    started: float,
    tail_limit: int,
) -> None:
    """Drain a child's standard error into ``state``, splitting it into lines

    Unlike stdout this decodes as it goes, because the frames it carries are
    line-oriented and must be available before the child exits. Undecodable
    bytes are replaced rather than raised: stderr is diagnostic output, and
    losing a run to a stray byte in it would be the wrong trade.

    Args:
        stream: The stream, or ``None`` when no pipe was requested.
        state: Accumulator mutated in place.
        diagnostics: Sink for progress records, or ``None``.
        started: Monotonic time the run began, for elapsed figures.
        tail_limit: Maximum characters to retain in ``state.tail``.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(STREAM_READ_CHUNK)
        if not chunk:
            break
        state.bytes_read += len(chunk)
        text = chunk.decode("utf-8", errors="replace")
        state.buffer += text
        while True:
            newline_index = state.buffer.find("\n")
            if newline_index < 0:
                break
            line = state.buffer[:newline_index].rstrip("\r")
            state.buffer = state.buffer[newline_index + 1 :]
            _consume_stderr_line(state, line, tail_limit=tail_limit)
        state.last_emit_time, state.last_emit_bytes = _maybe_emit_stream_progress(
            diagnostics,
            stream="stderr",
            bytes_read=state.bytes_read,
            started=started,
            last_emit_time=state.last_emit_time,
            last_emit_bytes=state.last_emit_bytes,
        )


async def _emit_worker_heartbeat(
    proc: WorkerProcess,
    stdout_state: _StdoutAccumulator,
    stderr_state: _StderrAccumulator,
    *,
    diagnostics: Diagnostics | None,
    started: float,
) -> None:
    """Emit a record every few seconds while the child is still running

    Without this a slow fetch is indistinguishable from a hung one in the
    diagnostics stream. Costs nothing when diagnostics are off, and returns
    immediately in that case rather than sleeping in a loop nobody reads.

    Args:
        proc: The running child. Only ``returncode`` is read, and its optional
            half is what this loop is spelled against.
        stdout_state: Accumulator, read for its byte count.
        stderr_state: Accumulator, read for its byte count.
        diagnostics: Sink, or ``None`` to return at once.
        started: Monotonic time the run began, for elapsed figures.
    """
    if diagnostics is None:
        return
    while proc.returncode is None:
        diagnostics.emit(
            "worker.heartbeat",
            "Worker heartbeat",
            {
                "stdout_bytes": stdout_state.bytes_read,
                "stderr_bytes": stderr_state.bytes_read,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
        await asyncio.sleep(STREAM_HEARTBEAT_INTERVAL_SECONDS)


async def _terminate_process_tree(proc: WorkerProcess) -> None:
    """Kill a child that has overrun, and on Windows its descendants too

    **The name overstates what this does on *both* platforms, and that is a
    known defect rather than a shorthand.**

    * Off Windows it signals the **direct child only**. Descendants are
      reparented and survive — probed: after the call, ``child alive=False
      grandchild alive=True PPid: 1``.
    * On Windows the first thing tried is ``proc.terminate()``, which CPython
      implements as ``TerminateProcess`` (and where ``kill`` is an *alias* for
      ``terminate``). That is immediate, unconditional, and also kills the named
      process only — Windows has no primitive for killing a tree.
      ``taskkill /T /F``, which does walk the tree, is reached only if the child
      is **still alive 1.5 s later**, and ``TerminateProcess`` makes that
      unlikely. So in the ordinary case descendants survive here too.

    In production that descendant is Chromium, so a timed-out worker leaves a
    browser and its profile directory behind, on **either** platform. The fix
    belongs to the worker-lifecycle step, whose verify clause already requires
    that a killed parent leave no orphan; a process group at spawn plus a group
    kill is the POSIX candidate, and a Win32 job object the Windows one. It is
    not fixed here because changing process termination is a behaviour change,
    and this step annotates signatures.

    Every failure is suppressed throughout. The process may exit between any two
    statements, and racing it is not an error worth propagating to a caller who
    is already handling a timeout.

    Args:
        proc: The child to kill. Returns immediately if it has already exited.
    """
    if proc.returncode is not None:
        return

    if os.name == "nt":
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(killer.wait(), timeout=2.0)
                if killer.returncode is None:
                    with contextlib.suppress(Exception):
                        killer.kill()
                if killer.returncode not in (0, None):
                    with contextlib.suppress(Exception):
                        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return

    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _run_worker_command(
    command: list[str],
    *,
    env: dict[str, str],
    default_timeout_seconds: float,
    diagnostics: Diagnostics | None,
) -> str:
    """Run a worker command to completion and return everything it wrote to stdout

    Spawns ``command`` with its standard output and standard error piped and its
    standard input closed, drains both streams concurrently while emitting
    progress and heartbeat diagnostics, waits for the process under a total
    timeout, and returns the decoded standard output. On timeout or cancellation
    the process tree is killed and the streams are drained before re-raising.

    The command is executed **exactly as given**. This function constructs no
    argv of its own, which is what lets a subsystem test point it at a fixture
    child instead of a browser. That seam is module-private on purpose: a public
    entry point accepting a caller-supplied command would turn "execute an
    arbitrary process" into a supported input of a code path whose url argument
    is already attacker-influenced.

    Args:
        command: The full argv of the child process, interpreter first.
        env: The complete environment handed to the child. It is not merged with
            the parent's — the caller has already decided what the child sees —
            and it is also where the timeout override below is read from, so
            this function reads no ambient state at all.
        default_timeout_seconds: Budget used when ``env`` sets no override. The
            ``KINDLY_HTML_TOTAL_TIMEOUT_SECONDS`` override is parsed here rather
            than by the caller so that the resolution and the
            ``worker.timeout_budget_parent`` record of it stay together, and so
            that record keeps its position in the diagnostics stream — after the
            process has started, where it has always been emitted.
        diagnostics: Sink for structured progress records, or ``None`` to run
            silently. Worker-side frames parsed off stderr are appended to it.

    Returns:
        The child's standard output, decoded as UTF-8 with undecodable bytes
        ignored.

    Raises:
        asyncio.TimeoutError: If the child outlives the resolved budget. The
            process tree is killed first.
        asyncio.CancelledError: If the caller is cancelled. The process tree is
            killed first.
        RuntimeError: If the child exits non-zero, carrying its stderr tail; or
            if the streams were never opened.
    """
    started = time.monotonic()
    proc: WorkerProcess = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **_subprocess_launch_options(),
    )
    stdout_state: _StdoutAccumulator | None = None
    stderr_state: _StderrAccumulator | None = None
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[int] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    if diagnostics:
        loop = asyncio.get_running_loop()
        policy = asyncio.get_event_loop_policy()
        diagnostics.emit(
            "worker.process_started",
            "Worker process started",
            {
                "pid": proc.pid,
                "event_loop": loop.__class__.__name__,
                "event_loop_policy": policy.__class__.__name__,
            },
        )

    try:
        # Read from the environment this call was handed, not from the ambient
        # one. Identical at the production call site -- the loader builds `env`
        # from `os.environ` and never writes this key -- and it makes the
        # function's inputs equal its signature, so a caller constructing an
        # environment for a child gets the budget it asked for.
        raw_timeout = (env.get("KINDLY_HTML_TOTAL_TIMEOUT_SECONDS") or "").strip()
        used_default = False
        invalid = False
        parsed_value = default_timeout_seconds
        try:
            if raw_timeout:
                parsed_value = float(raw_timeout)
            else:
                used_default = True
        except ValueError:
            used_default = True
            invalid = True
        if parsed_value <= 0:
            used_default = True
            invalid = True
            parsed_value = default_timeout_seconds
        clamped = False
        timeout_seconds = max(1.0, min(parsed_value, 600.0))
        clamped = timeout_seconds != parsed_value
        if diagnostics:
            diagnostics.emit(
                "worker.timeout_budget_parent",
                "Resolved worker timeout budget",
                {
                    "raw_value": raw_timeout,
                    "clamped_value": timeout_seconds,
                    "effective_timeout_seconds": timeout_seconds,
                    "clamped": clamped,
                    "used_default": used_default,
                    "invalid": invalid,
                    "default_seconds": default_timeout_seconds,
                },
            )
        stdout_state = _StdoutAccumulator()
        stderr_state = _StderrAccumulator()
        stdout_task = asyncio.create_task(
            _read_stdout_stream(
                proc.stdout, stdout_state, diagnostics=diagnostics, started=started
            )
        )
        stderr_task = asyncio.create_task(
            _read_stderr_stream(
                proc.stderr,
                stderr_state,
                diagnostics=diagnostics,
                started=started,
                tail_limit=MAX_STDERR_CHARS,
            )
        )
        heartbeat_task = asyncio.create_task(
            _emit_worker_heartbeat(
                proc,
                stdout_state,
                stderr_state,
                diagnostics=diagnostics,
                started=started,
            )
        )
        wait_task = asyncio.create_task(proc.wait())
        await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, heartbeat_task, wait_task),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        for task in (stdout_task, stderr_task, heartbeat_task, wait_task):
            if task is not None:
                task.cancel()
        for task in (stdout_task, stderr_task, heartbeat_task, wait_task):
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await _terminate_process_tree(proc)
        if stderr_state is not None:
            _finalize_stderr_state(stderr_state, tail_limit=MAX_STDERR_CHARS)
            if diagnostics and stderr_state.worker_entries:
                diagnostics.entries.extend(stderr_state.worker_entries)
            if diagnostics and stderr_state.parse_errors:
                diagnostics.emit(
                    "worker.diag_parse_error",
                    "Failed to parse worker diagnostics",
                    {"samples": stderr_state.parse_errors},
                )
        if diagnostics:
            stderr_tail = stderr_state.tail if stderr_state is not None else ""
            stdout_len = stdout_state.bytes_read if stdout_state is not None else 0
            stderr_sample, stderr_truncated, stderr_len = truncate_text(
                stderr_tail, MAX_STDERR_CHARS
            )
            diagnostics.emit(
                "worker.timeout",
                "Nodriver worker timed out",
                {
                    "timeout_seconds": timeout_seconds,
                    "runtime_ms": int((time.monotonic() - started) * 1000),
                    "stderr_len": stderr_len,
                    "stderr_sample": stderr_sample,
                    "stderr_truncated": stderr_truncated,
                    "stdout_len": stdout_len,
                },
            )
        raise
    except asyncio.CancelledError:
        for task in (stdout_task, stderr_task, heartbeat_task, wait_task):
            if task is not None:
                task.cancel()
        for task in (stdout_task, stderr_task, heartbeat_task, wait_task):
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await _terminate_process_tree(proc)
        if diagnostics:
            diagnostics.emit("worker.cancelled", "Nodriver worker cancelled", {})
        raise

    if stderr_state is None or stdout_state is None:
        raise RuntimeError("nodriver worker streams unavailable")

    _finalize_stderr_state(stderr_state, tail_limit=MAX_STDERR_CHARS)
    if diagnostics and stderr_state.worker_entries:
        diagnostics.entries.extend(stderr_state.worker_entries)
    if diagnostics and stderr_state.parse_errors:
        diagnostics.emit(
            "worker.diag_parse_error",
            "Failed to parse worker diagnostics",
            {"samples": stderr_state.parse_errors},
        )

    if proc.returncode != 0:
        detail = stderr_state.tail
        if diagnostics:
            stderr_sample, stderr_truncated, stderr_len = truncate_text(
                detail, MAX_STDERR_CHARS
            )
            diagnostics.emit(
                "worker.exit",
                "Nodriver worker failed",
                {
                    "exit_code": proc.returncode,
                    "stderr_len": stderr_len,
                    "stderr_sample": stderr_sample,
                    "stderr_truncated": stderr_truncated,
                    "runtime_ms": int((time.monotonic() - started) * 1000),
                },
            )
        raise RuntimeError(
            f"nodriver worker failed (exit={proc.returncode}): {detail or 'unknown error'}"
        )

    if diagnostics:
        if stderr_state.tail:
            stderr_sample, stderr_truncated, stderr_len = truncate_text(
                stderr_state.tail, MAX_STDERR_CHARS
            )
            diagnostics.emit(
                "worker.stderr",
                "Nodriver worker stderr output",
                {
                    "stderr_len": stderr_len,
                    "stderr_sample": stderr_sample,
                    "stderr_truncated": stderr_truncated,
                    "runtime_ms": int((time.monotonic() - started) * 1000),
                },
            )
        diagnostics.emit(
            "worker.stdout",
            "Nodriver worker completed",
            {
                "stdout_len": stdout_state.bytes_read,
                "runtime_ms": int((time.monotonic() - started) * 1000),
            },
        )

    return bytes(stdout_state.buffer).decode("utf-8", errors="ignore")
