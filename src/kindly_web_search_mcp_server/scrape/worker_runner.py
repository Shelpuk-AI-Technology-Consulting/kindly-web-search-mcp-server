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

The helpers below moved here verbatim from ``universal_html.py``. They are
unchanged, deliberately: an extraction that also rewrites what it moves cannot
be reviewed as an extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ..utils.diagnostics import (
    MAX_STDERR_CHARS,
    Diagnostics,
    truncate_text,
)


@dataclass
class _StdoutAccumulator:
    buffer: bytearray = field(default_factory=bytearray)
    bytes_read: int = 0
    last_emit_time: float = 0.0
    last_emit_bytes: int = 0


@dataclass
class _StderrAccumulator:
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
    if os.name != "nt":
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
    if not addition:
        return existing
    combined = existing + addition
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


def _consume_stderr_line(
    state: _StderrAccumulator, line: str, *, tail_limit: int
) -> None:
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
    proc: asyncio.subprocess.Process,
    stdout_state: _StdoutAccumulator,
    stderr_state: _StderrAccumulator,
    *,
    diagnostics: Diagnostics | None,
    started: float,
) -> None:
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


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    if os.name == "nt":
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        if proc.returncode is None and proc.pid is not None:
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
    proc = await asyncio.create_subprocess_exec(
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
