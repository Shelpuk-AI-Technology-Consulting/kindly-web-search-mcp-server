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

**:func:`_terminate_process_tree` used to be this rule's example and is now its
counter-example.** It was ``os.name``-guarded for exactly the reason above, and
the price of converting it was measured: with ``sys.platform``, mypy treats its
whole ``taskkill`` path as unreachable on Linux and stops checking it there —
an injected error is reported under ``os.name`` and not under ``sys.platform``.
It converted anyway, because the tree walk added to its POSIX branch reads
``os.killpg``, ``os.getpgid`` and ``signal.SIGKILL``, all POSIX-only in
typeshed, and the first rule outranks the second whenever both apply. The
``--platform win32`` invocation is now that branch's only reader, which is what
makes that invocation load-bearing rather than a belt-and-braces extra.

The rule to take from the pair: **ask which stdlib the branch bodies touch, not
which reads more tidily.** A function acquires a platform-exclusive call long
after its guard was written, and the guard does not announce that it has gone
stale.

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
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
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
#: How long the Windows branch waits for the worker to die, after `taskkill` and
#: again after the `terminate()` fallback. One constant for both, because they
#: are the same question asked twice and a reader comparing two literals cannot
#: tell a deliberate difference from a typo. Was an inline `1.5` on the first
#: wait and nothing at all on the second.
TERMINATE_WAIT_SECONDS = 1.5

#: Ceiling on retrying the removal of a worker's profile directory. Bounded
#: because it runs while a caller is already unwinding; generous because the
#: alternative to waiting is leaking the directory permanently.
PROFILE_CLEANUP_TIMEOUT_SECONDS = 5.0

#: One retry slice for that removal.
PROFILE_CLEANUP_RETRY_SECONDS = 0.1

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


async def _remove_worker_profile_directory(
    path: str, *, diagnostics: Diagnostics | None
) -> None:
    """Delete a profile directory the parent created for a worker

    Killing the process tree does not delete this: a directory is not a
    process, and the worker that would have removed its own was ``SIGKILL``ed
    without running a single finalizer. The parent creates it and the parent
    removes it, on every path a fetch can end on.

    **Retried, because the descendants were signalled and not waited for.**
    :func:`_terminate_process_tree` awaits the *worker*; a browser holding files
    open in here may take a moment longer to go, and on Windows an open handle
    makes the delete fail outright rather than merely orphaning an inode. One
    attempt would therefore leak on exactly the platform this fix is least able
    to observe.

    **Never raised, and never silent.** The docstring beside the worker's own
    copy of this cleanup records that a profile directory which cannot be
    deleted must not fail the request; and raising here would replace a caller's
    ``CancelledError`` with an ``OSError`` from a ``finally``. But swallowing it
    would leave the disk half of a leak invisible, which is the failure mode
    this whole change exists to end — so a directory that survives is reported.

    This lives in the runner rather than beside its caller because the retry
    needs to ``await`` between attempts, and the loader is forbidden to import
    :mod:`asyncio` at all: that import's absence is how the module boundary is
    asserted.

    **Two costs, so they are decisions rather than surprises.** This can add up
    to :data:`PROFILE_CLEANUP_TIMEOUT_SECONDS` to a request whose budget has
    already expired; and a *second* cancellation delivered at the sleep below
    escapes the caller's ``finally``, leaking the directory with no record — the
    one outcome this function exists to prevent. Both are accepted: the
    alternative to waiting is leaking on Windows every time, and a shield
    against re-cancellation here would have to swallow ``CancelledError``, which
    is worse than the leak it would prevent.

    Args:
        path: The directory to remove.
        diagnostics: Sink for the record written when removal fails, or ``None``.
    """
    deadline = time.monotonic() + PROFILE_CLEANUP_TIMEOUT_SECONDS
    last_error = ""
    while True:
        try:
            shutil.rmtree(path)
            return
        # Already gone is success, not an error worth retrying for five seconds.
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = type(exc).__name__
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(PROFILE_CLEANUP_RETRY_SECONDS)

    if diagnostics:
        diagnostics.emit(
            "worker.profile_dir_retained",
            "Worker profile directory could not be removed",
            {
                "path": path,
                "error": last_error,
                "waited_seconds": PROFILE_CLEANUP_TIMEOUT_SECONDS,
            },
        )


def _parse_parent_pid(stat_text: str) -> int | None:
    """Read a process's parent pid out of one ``/proc/<pid>/stat`` line

    The second field of that line is the executable name in brackets, and the
    kernel neither escapes nor rejects spaces or a closing bracket inside it.
    Splitting the line on whitespace therefore reads some other field entirely.
    Against a name containing a space and a digit that is *silent*: it yields a
    plausible small integer rather than an error, and this module would go on to
    treat it as a process it may ``SIGKILL`` and as a process group it may
    signal. Everything after the **last** bracket is read for that reason.

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


def _read_parent_map() -> dict[int, int]:
    """Snapshot every visible process's parent, from ``/proc``

    Reading every ``stat`` file is O(all processes) where
    ``/proc/<pid>/task/<tid>/children`` would be O(descendants). The latter is
    deliberately not used: enumerating a process's children through it means
    first enumerating that process's *threads*, which weakens the atomicity of
    the snapshot across the iteration, and it is a narrower interface than
    ``stat``. The scan is a few hundred small reads and happens only when a
    fetch is already being torn down.

    **Synchronous file I/O only, with no ``await`` anywhere.** This runs inside
    :func:`_run_worker_command`'s ``except asyncio.CancelledError`` handler,
    where a second cancellation arriving at an ``await`` would raise before
    anything had been killed — returning the exact leak this function exists to
    close, on the exact path it targets. That is also why no ``ps`` subprocess
    fallback exists for platforms without ``/proc``: there, this returns nothing
    and the worker is still killed by pid, which is the behaviour that shipped
    before.

    **The claim is "no *additional* await", not "no await on this path".** That
    handler already awaits four stream tasks before it reaches the terminator,
    and a re-cancellation delivered at any of those does what this paragraph
    describes. Closing that window means killing before draining, which reorders
    the diagnostics both sides of the seam pin and is a behaviour change this
    walk has no business smuggling in. Recorded rather than fixed, because the
    difference between "we closed it" and "we did not widen it" is the whole
    value of writing it down.

    Returns:
        Each visible pid mapped to its parent's pid. Empty when ``/proc`` cannot
        be read at all, which is every non-Linux platform.
    """
    parent_map: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return parent_map
    for entry in entries:
        # `isdecimal`, not `isdigit`: the latter is true for characters like
        # 'squared' that `int()` then rejects. Unreachable on procfs, and this
        # function is contracted never to raise into a cancellation handler.
        if not entry.isdecimal():
            continue
        # One unreadable entry costs its own row and nothing else: processes are
        # exiting underneath this loop by definition, and abandoning the scan
        # over one of them could abandon the browser.
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as handle:
                stat_text = handle.readline()
        except OSError:
            continue
        parent_pid = _parse_parent_pid(stat_text)
        if parent_pid is not None:
            parent_map[int(entry)] = parent_pid
    return parent_map


def _collect_descendants(parent_map: Mapping[int, int], root: int) -> list[int]:
    """List every transitive descendant of ``root``

    Transitive, not just children: in production the worker's child is Chromium
    and Chromium's children are its renderers, so a walk that stopped at the
    first generation would reproduce the defect one level deeper.

    Args:
        parent_map: Each pid mapped to its parent's pid, from
            :func:`_read_parent_map`.
        root: The process whose descendants are wanted. It is never included.

    Returns:
        The descendants, in no particular order.
    """
    children: dict[int, list[int]] = {}
    for pid, parent_pid in parent_map.items():
        children.setdefault(parent_pid, []).append(pid)

    # `seen` is not an optimisation. The map is assembled from many files that
    # were not read atomically while pids were being recycled, so it can contain
    # a cycle -- and a hang here would stall the cancellation handler silently.
    descendants: list[int] = []
    seen = {root}
    queue = list(children.get(root, ()))
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        queue.extend(children.get(pid, ()))
    return descendants


def _signal_descendants(
    descendants: Sequence[int],
    *,
    own_pid: int,
    own_group: int,
    signal_number: int,
    getpgid: Callable[[int], int],
    killpg: Callable[[int, int], None],
    kill: Callable[[int, int], None],
) -> None:
    """Signal a worker's descendants, by process group and then individually

    Two passes, and both are needed.

    The **group** pass is what reaches a browser cheaply: Chromium is launched
    with ``start_new_session`` on POSIX, so it leads its own group and one call
    takes it together with every renderer it started, including renderers
    spawned after the walk that produced ``descendants``.

    The **per-pid** pass is the general claim the group pass optimises. A
    descendant that did not call ``setsid`` is in the *caller's* group, which
    the exclusion below skips, so nothing else reaches it.

    **The own-group exclusion is not defensive.** The worker is spawned with no
    ``start_new_session`` (see :func:`_subprocess_launch_options`), so it shares
    the server's process group and so does any descendant that did not detach.
    Without the exclusion this reaches ``killpg(<the server's own group>)`` on an
    ordinary cancelled fetch and takes the server down with the browser. The
    matching exclusions on ``own_pid`` and pid 1 guard the same class of
    mistake: a walk rooted at the worker cannot reach either unless the snapshot
    contained a cycle or a recycled pid, both of which are admitted.

    Every signal is dependency-injected so this can be driven with no process in
    existence. That is not a general seam for this module — it is specific to
    the one function whose obvious mutation, if run against a real process tree,
    kills the test session and its shell and produces no report at all.

    Args:
        descendants: Pids to signal, from :func:`_collect_descendants`.
        own_pid: The calling process, which must never be signalled.
        own_group: The calling process's group, which must never be signalled.
        signal_number: Signal to send. Production sends ``SIGKILL``; by the time
            this runs the caller has already given up on the worker.
        getpgid: Resolves a pid's process group; ``os.getpgid`` in production.
        killpg: Signals a process group; ``os.killpg`` in production.
        kill: Signals a process; ``os.kill`` in production.
    """
    # Groups first, and deduplicated: every renderer of one browser resolves to
    # the same group, and one call covers them all.
    groups: list[int] = []
    for pid in descendants:
        if pid == own_pid or pid == 1:
            continue
        try:
            group = getpgid(pid)
        except OSError:
            continue
        if group in (own_group, 0) or group in groups:
            continue
        groups.append(group)
    for group in groups:
        with contextlib.suppress(OSError):
            killpg(group, signal_number)

    # Then individually, which is what reaches a descendant whose group was
    # excluded -- the ordinary case for anything that did not detach itself.
    for pid in descendants:
        if pid == own_pid or pid == 1:
            continue
        with contextlib.suppress(OSError):
            kill(pid, signal_number)


async def _terminate_process_tree(proc: WorkerProcess) -> None:
    """Kill a worker that has overrun, and every process it started

    **Order is the whole design: enumerate first, signal second.** A process's
    children are reparented to ``init`` the instant it dies, so a walk performed
    after the kill finds nothing and the descendants become unattributable
    strays. In production those descendants are a headless Chromium and its
    renderers.

    Off Windows:

    1. Snapshot the worker's transitive descendants from ``/proc``
       (:func:`_read_parent_map`, :func:`_collect_descendants`).
    2. ``killpg`` each distinct group among them, then ``kill`` each of them
       individually — see :func:`_signal_descendants` for why both passes are
       needed and which exclusions are load-bearing.
    3. Kill the worker **by pid**, never by group.

    On Windows ``taskkill /T /F`` runs **first**. It is the only thing on the
    platform that walks a tree: CPython implements ``Process.terminate`` as
    ``TerminateProcess`` and aliases ``kill`` to it, which is immediate,
    unconditional, and reaches the named process only. The fallback is keyed on
    ``proc.returncode`` — *did our own worker die* — and deliberately **not** on
    ``taskkill``'s exit status, which is 128 whenever any member of the tree had
    already exited, an ordinary outcome for a browser whose renderers churn.

    **Two rejected alternatives, both of which read as obviously correct.**

    * *A process group at the worker's spawn* (``start_new_session=True``, then
      ``os.killpg`` of the worker's group). It does not reach the browser:
      :func:`~kindly_web_search_mcp_server.scrape.nodriver_worker._launch_chromium`
      already starts Chromium with ``start_new_session`` on POSIX, so the
      browser leads a session of its own and is in no group the worker belongs
      to. Measured. Worse, written against today's spawn it is *destructive*:
      :func:`_subprocess_launch_options` returns no such option, so the worker
      shares the **server's** process group and that call signals the server.
      It also costs something — detaching the worker would stop a group
      ``SIGINT`` reaching it, and the worker's ``KeyboardInterrupt`` path is
      what runs its own browser teardown and profile cleanup today.
    * *A Win32 job object* with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. It is
      the platform's real equivalent of a process group, and it needs ``ctypes``
      against ``kernel32`` plus a handle held for the worker's whole lifetime —
      a spawn-side change to a module with no hermetic seam, on a platform with
      no standing test lane. ``taskkill`` was already here and needed only to be
      tried first.

    **Known limits, so silence is not mistaken for success.** This function
    reports nothing, so both of the following are invisible until it gains a
    diagnostics sink.

    * Where ``/proc`` does not exist the walk yields nothing and behaviour
      degrades to killing the worker alone — which is what shipped before.
    * A Windows process stuck in unprocessed I/O defeats ``taskkill /F`` and
      ``TerminateProcess`` alike, and this returns having killed nothing. It
      **returns** rather than blocking because both of that branch's waits are
      bounded by :data:`TERMINATE_WAIT_SECONDS`; the closing one was unbounded
      until three reviews in a row said so. The POSIX branch's closing wait is
      deliberately not bounded to match: there ``SIGKILL`` has already been
      delivered and cannot be caught or ignored, so the kernel bounds it.

    Every failure that can be raced is suppressed: the process may exit between
    any two statements, and racing it is not an error worth propagating to a
    caller already handling a timeout. The two walk calls below are the
    exception and need no suppression of their own — each catches its own
    ``OSError`` internally, per entry and per signal, so one unreadable process
    costs its own row rather than the whole kill.

    Args:
        proc: The child to kill. Returns immediately if it has already exited.
    """
    if proc.returncode is not None:
        return

    # `sys.platform`, not `os.name`: `os.killpg`, `os.getpgid` and
    # `signal.SIGKILL` below are POSIX-only in typeshed, and mypy narrows on
    # this spelling alone. The cost is that the Windows body stops being checked
    # natively, which is what the `--platform win32` invocation is for.
    if sys.platform == "win32":
        # First, not last. This is the platform's only tree walk; leaving it as
        # a fallback behind `terminate()` -- which always succeeds -- is what
        # made the Windows half of this function a no-op for descendants.
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
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_WAIT_SECONDS)
        # Keyed on our own worker, never on `taskkill`'s exit code: 128 means
        # "something in the tree had already gone", not "the tree survived".
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()
        # Bounded, unlike the POSIX branch's closing wait, and the asymmetry is
        # the point. There `SIGKILL` has already been delivered and cannot be
        # caught or ignored, so the kernel bounds the wait. Here the process
        # that defeated `taskkill /F` is the same one `TerminateProcess` may
        # fail to signal, and an unbounded wait would park the event loop inside
        # a cancellation handler -- a hang on the path handling a fetch the
        # caller has already given up on, which is worse than the leak this
        # function exists to close. Returning without reaping is the lesser
        # cost.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_WAIT_SECONDS)
        return

    # Before any signal. After `proc.kill()` these pids are children of `init`
    # and no walk can attribute them to this worker again.
    descendants = _collect_descendants(_read_parent_map(), proc.pid)
    _signal_descendants(
        descendants,
        own_pid=os.getpid(),
        own_group=os.getpgid(0),
        signal_number=signal.SIGKILL,
        getpgid=os.getpgid,
        killpg=os.killpg,
        kill=os.kill,
    )

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
