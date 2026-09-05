"""A stand-in for the nodriver worker, driven entirely by its command line.

This script is **test-only**. It is spawned by path, the way the real worker is
spawned by module, so that the parent-side subprocess
machinery in ``kindly_web_search_mcp_server.scrape.worker_runner`` can be
exercised against a real process whose behaviour the test chooses.

It imports nothing but the standard library, and in particular does not import
the package under test. That is not a purity preference, it is what makes the
script startable at all in the situations it is used in. Two concrete reasons:

- ``worker_runner._run_worker_command`` hands its child a **complete**
  environment rather than merging one into the parent's, so whether an import of
  the package resolves would depend on whatever path setup that environment
  happened to carry;
- the suite reaches ``src/`` only through the ``sys.path`` insertion in
  ``tests/conftest.py``, and a script invoked by path never executes a conftest.

A weaker third reason is often given for fixtures like this one -- that importing
the code under test lets a defect hide -- and it should not be leaned on here,
because the fidelity of these frames is pinned to the production decoder anyway
(see the smoke test's decoder case).

Nothing in the suite imports this file to *use* it. Three call sites across two
test modules load it to re-derive values defined here -- the standard-output
pattern, and the descendant program a reaping case needs a look-alike of -- which
is safe because everything here is under the ``__main__`` guard, and is
preferable to copies: two copies edited together catch drift and never deletion.

Its behaviours are **orthogonal flags**, not exclusive modes, because the
lifecycle tests need combinations: "emit frames and then hang" is how the claim
that a timed-out run still yields the frames received before the deadline gets
exercised. They are applied in a fixed order:

1. spawn a descendant chain, if asked, and wait for every generation of it;
2. write the pid file, if asked;
3. emit the readiness frame on standard error and flush it;
4. emit each requested frame;
5. write the requested standard-error garbage;
6. write the requested standard-output payload;
7. hang, if asked, until signalled;
8. exit with the requested code.

Step 1 gained its wait with ``--grandchild-depth``. Generations beyond the first
are spawned by processes this script does not supervise, so without it the
readiness frame would arrive while the chain was still being built and the
guarantee in the next paragraph would hold only for a chain of one.

The descendant chain is spawned **and awaited** before readiness is announced,
so a harness that reaps the process tree the moment it sees the readiness frame
can never observe a half-built tree. If the chain does not complete within
:data:`CHAIN_TIMEOUT_SECONDS` the script says so in a frame and exits
:data:`CHAIN_INCOMPLETE_EXIT_CODE` rather than announcing a tree that is not
there -- and rather than hanging, which would surface as the caller's readiness
timeout and name neither the chain nor the count.

The pid file is written in that same window, and for a
stronger reason: a parent that is **cancelled** never receives this script's
frames at all -- ``_run_worker_command`` appends them to the caller's
diagnostics on the timeout path and on no other -- so the file is the only
channel on which a cancelled run can learn which processes must be gone.

**Every byte this script writes goes through a stream's ``.buffer`` and is
flushed.** Standard error is block-buffered behind a pipe, so an unflushed
readiness frame would not arrive until exit; and a Windows console codepage
would otherwise decide the encoding of payloads the parent decodes as UTF-8.

**Nothing here waits forever.** Both the hang and the descendant are bounded --
see :data:`MAX_LIFETIME_SECONDS`. The bound is a leak backstop for the case where
the reaping harness never runs at all, not a synchronization point: no test may
depend on it firing.

Two combinations the fixed order makes vacuous, named so nobody plans a test
around them: ``--hang`` reaches step 6 and never returns, so ``--exit-code`` is
unreachable alongside it, and a ``--stdout`` payload is written before the hang
rather than during it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

#: Prefix the parent's line router uses to tell a deliberate frame from ordinary
#: child output. Hard-coded rather than imported: see the module docstring.
FRAME_PREFIX = "KINDLY_DIAG "

#: Stage of the frame that announces the script is running and set up.
READY_STAGE = "fixture.ready"

#: The ordinary, non-frame line the garbage mode writes. The smoke test asserts
#: this exact text reached the parent's stderr tail.
GARBAGE_PLAIN_LINE = "chrome: ordinary noise on stderr"

#: Ceiling on how long this script and its descendant stay alive with nobody
#: reaping them. Matched to the 300 seconds ``tests/test_baseline_failure_ledger``
#: allows its own child, and far past every deadline any caller sets.
#:
#: It exists because the reaping harness does not always run: a test runner that
#: is killed outright, a cancelled CI job, or a crash between spawn and the
#: ``try`` block all skip the cleanup and would otherwise leave an immortal Python
#: process -- two of them with ``--spawn-grandchild`` -- on a developer's machine.
#: A test must never wait for this to fire; anything that did would be asserting
#: on a backstop rather than on behaviour.
MAX_LIFETIME_SECONDS = 300.0

#: Length of one sleep slice. A deadline loop needs a granularity, and this is
#: it -- nothing more. An earlier draft justified it as making the sleep
#: interruptible on Windows, which is not true of any Python this project
#: supports: CPython's Windows ``time.sleep`` waits on the SIGINT event and is
#: already interrupted by Ctrl-C. Nothing here sends a console control event
#: either; the reaper uses ``taskkill /F`` and ``SIGKILL``, neither of which a
#: sleep can delay.
_SLEEP_SLICE_SECONDS = 0.25

#: One slice of the chain wait's poll. Much shorter than
#: :data:`_SLEEP_SLICE_SECONDS`, and the difference is load-bearing rather than
#: cosmetic: a whole generation of this chain starts and records itself in
#: roughly 25 ms, so a quarter-second slice lets a wait that stops one
#: generation early still observe the complete chain on its next read. Measured
#: -- with a quarter-second slice, a wait mutated to expect one generation
#: instead of three returned all three anyway and the calibration case passed.
_CHAIN_POLL_SECONDS = 0.01

#: Ceiling on waiting for every generation of a descendant chain to report. Well
#: under the 30 seconds a caller allows for readiness, and deliberately so: the
#: two failures need different diagnoses, and a chain bound at or above the
#: readiness budget would let scheduling decide which of them a reader is shown.
CHAIN_TIMEOUT_SECONDS = 10.0

#: Exit status when the chain does not complete in time. Not ``2``, which
#: ``argparse`` already spends on an unknown flag: a chain failure that read as a
#: bad command line would send the next reader to the command line.
CHAIN_INCOMPLETE_EXIT_CODE = 70

#: Stage of the frame that reports a chain which never finished building.
CHAIN_INCOMPLETE_STAGE = "fixture.chain_incomplete"

#: Signal used to reap what this script started. ``SIGKILL`` where it exists;
#: on Windows there is none and any value other than the two console-control
#: events makes ``os.kill`` call ``TerminateProcess``, which is unconditional.
_REAP_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def stdout_pattern_byte(index: int) -> int:
    """Give the byte at ``index`` of a generated standard-output payload.

    A function rather than a literal so there is **one** source: a pattern
    duplicated between this script and the case that checks it would be two
    things edited together, which catches drift and never deletion -- a
    generator quietly replaced by a run of zeros satisfies two agreeing copies.

    The multiplier and the modulus are coprime and the period is 251 bytes, so
    no offset shift, truncation or repeated block reproduces the same sequence.

    Args:
        index: Position in the payload, from zero.

    Returns:
        The byte value at that position.
    """
    return (index * 31 + 7) % 251


#: Monotonic clock origin for the ``elapsed_ms`` field, seeded at import so the
#: readiness frame reports the time from interpreter start, not from first use.
_STARTED = time.monotonic()


def _emit_frame(stage: str, msg: str, data: dict[str, object] | None = None) -> None:
    """Write one ``KINDLY_DIAG`` frame to standard error and flush it.

    The field set matches the real worker's emitter -- ``request_id``, ``stage``,
    ``msg``, ``elapsed_ms``, ``data`` -- because the parent merges these frames
    into the same diagnostics stream as the worker's own.

    Written through ``sys.stderr.buffer`` as UTF-8 and flushed on every frame.
    Both halves are load-bearing: standard error is block-buffered behind a pipe,
    so an unflushed readiness frame would not arrive until exit, and a Windows
    console codepage would otherwise decide the encoding of a payload the parent
    decodes as UTF-8.

    Args:
        stage: Short identifier for what the script was doing.
        msg: Human-readable description.
        data: Structured payload, or ``None`` for an empty one.
    """
    entry = {
        "request_id": os.environ.get("KINDLY_REQUEST_ID", "fixture"),
        "stage": stage,
        "msg": msg,
        "elapsed_ms": int((time.monotonic() - _STARTED) * 1000),
        "data": data or {},
    }
    payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
    line = (FRAME_PREFIX + payload).rstrip() + "\n"
    sys.stderr.buffer.write(line.encode("utf-8"))
    sys.stderr.buffer.flush()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the script's command line.

    Args:
        argv: Arguments after the program name.

    Returns:
        The parsed flags.
    """
    parser = argparse.ArgumentParser(
        description="Test-only stand-in for the nodriver worker."
    )
    parser.add_argument(
        "--emit-frame",
        action="append",
        default=[],
        metavar="STAGE",
        help="Emit a diagnostic frame with this stage. Repeatable, in order.",
    )
    parser.add_argument(
        "--stdout",
        default="",
        metavar="TEXT",
        help="Write TEXT to standard output as UTF-8, with nothing added.",
    )
    parser.add_argument(
        "--stderr-garbage",
        action="store_true",
        help="Write each malformation the parent's line router must survive.",
    )
    parser.add_argument(
        "--spawn-grandchild",
        action="store_true",
        help="Start a descendant that outlives this process unless killed.",
    )
    parser.add_argument(
        "--grandchild-new-session",
        action="store_true",
        help="Give the descendant its own session, the way a browser is launched.",
    )
    parser.add_argument(
        "--grandchild-depth",
        type=int,
        # A sentinel rather than `1`, so an explicit `--grandchild-depth 1` can
        # be told from the default. With `1` as the default the check below
        # cannot fire for it, and the flag is silently ignored in exactly the
        # case a caller is most likely to try first.
        default=None,
        metavar="N",
        help="Length of the descendant chain; needs --spawn-grandchild.",
    )
    parser.add_argument(
        "--chain-timeout",
        type=float,
        default=CHAIN_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="Ceiling on waiting for the chain to report itself.",
    )
    parser.add_argument(
        "--stdout-bytes",
        type=int,
        default=0,
        metavar="N",
        help="Write N generated bytes to standard output.",
    )
    parser.add_argument(
        "--pid-file",
        default=None,
        metavar="PATH",
        help="Write this process's pid and its descendant's to PATH as JSON.",
    )
    parser.add_argument(
        "--hang",
        action="store_true",
        help="Block after all other output until the process is signalled.",
    )
    parser.add_argument(
        "--exit-code",
        type=int,
        default=0,
        metavar="N",
        help="Exit with this status once everything else is done.",
    )
    parsed = parser.parse_args(argv)
    # Rejected rather than ignored. A depth silently doing nothing because the
    # flag that arms it was forgotten is the shape of an afternoon lost to a
    # tree that was never built, and `argparse`'s exit 2 is a diagnosis a caller
    # already knows how to read.
    if parsed.grandchild_depth is not None and not parsed.spawn_grandchild:
        parser.error("--grandchild-depth needs --spawn-grandchild")
    if parsed.grandchild_depth is None:
        parsed.grandchild_depth = 1
    elif parsed.grandchild_depth < 1:
        parser.error("--grandchild-depth must be at least 1")
    return parsed


def _write_stderr_garbage() -> None:
    """Write all four shapes of stderr output the parent must survive.

    Written together rather than as four flags because they are one claim -- a
    parent that copes with real child output copes with all of them -- and
    because the order they arrive in is part of what is being reproduced: a real
    browser interleaves its own noise with the worker's frames.

    The shapes, and where each must land in the parent:

    1. an ordinary line, which becomes stderr tail;
    2. a frame whose payload is not JSON, sampled as a parse error;
    3. a frame whose payload is JSON but not an object, sampled likewise;
    4. bytes that are not valid UTF-8, which the parent replaces rather than
       raising on -- the one shape that would turn a diagnostic into a crash.

    Written as raw bytes throughout, because shape 4 cannot be expressed as a
    ``str`` at all.
    """
    lines = [
        GARBAGE_PLAIN_LINE.encode("utf-8"),
        (FRAME_PREFIX + '{"stage": "truncated"').encode("utf-8"),
        (FRAME_PREFIX + '"a string, not an object"').encode("utf-8"),
        # A bare 0xFF followed by a lone continuation byte: neither can begin a
        # valid UTF-8 sequence, so a strict decode raises and a replacing one
        # does not.
        b"\xff\x80 undecodable browser noise",
    ]
    for line in lines:
        sys.stderr.buffer.write(line + b"\n")
    sys.stderr.buffer.flush()


#: The descendant program, as source. Passed to ``python -c`` and then handed to
#: itself as its own first argument, which is what lets one generation start the
#: next without a second file: ``python -c CODE a b`` leaves ``sys.argv`` as
#: ``["-c", "a", "b"]``, so the code is not otherwise recoverable from inside.
#:
#: ``-c`` rather than a second copy of this script: a descendant needs to do
#: nothing but record itself, start the next generation and stay alive, and
#: giving it this script's flags would invite it to grow behaviour nothing asked
#: for.
#:
#: **It records itself before it spawns the next generation**, so a count of
#: records reaching N proves every one of the N processes exists. It reports its
#: *own* view of its parent, which is what lets a calibration case assert the
#: chain's shape without a process-table lookup -- and the only process table in
#: this tree belongs to the harness these descendants exist to measure.
#:
#: Written whole under a temporary name and then ``os.replace``d into place: the
#: reader counts files, and an append shared by N processes is not atomic on
#: Windows, so a reader could otherwise count a half-written line.
#:
#: Its lifetime carries the same backstop as the hang, because these are the
#: processes most likely to be left behind.
_DESCENDANT_SOURCE = (
    "import json, os, subprocess, sys, time\n"
    "code, record_dir = sys.argv[1], sys.argv[2]\n"
    "generation, remaining = int(sys.argv[3]), int(sys.argv[4])\n"
    "record = os.path.join(record_dir, str(generation) + '.json')\n"
    "with open(record + '.tmp', 'w', encoding='utf-8') as handle:\n"
    "    json.dump({'pid': os.getpid(), 'ppid': os.getppid(),\n"
    "               'generation': generation}, handle)\n"
    "os.replace(record + '.tmp', record)\n"
    "if remaining > 1:\n"
    "    subprocess.Popen([sys.executable, '-c', code, code, record_dir,\n"
    "                      str(generation + 1), str(remaining - 1)],\n"
    "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "                     stderr=subprocess.DEVNULL)\n"
    f"deadline = time.monotonic() + {MAX_LIFETIME_SECONDS}\n"
    "while time.monotonic() < deadline:\n"
    f"    time.sleep({_SLEEP_SLICE_SECONDS})\n"
)


def _chain_record_directory(pid_file: str | None) -> str:
    """Choose where the chain's generations record themselves.

    **Beside the caller's pid file whenever there is one**, because then the
    caller owns the directory and something else cleans it: pytest's ``tmp_path``
    removes the whole tree after the test, whenever and however this process
    died.

    The window this closes is narrower than it first looks, and the narrower
    claim is the measured one. The removal below runs as soon as the chain is
    complete -- **not** at process exit -- so a child killed later, which is what
    every reaping case does, has already cleaned up: those cases leaked nothing
    even with ``tempfile.mkdtemp``, measured. What does leak is a child killed
    *during* the chain wait, before the removal is reached: **5 of 10 runs** left
    a directory behind.

    A caller with no pid file still gets a temporary directory, and still has
    that window. Nothing in the suite takes that path; it is for a developer
    running the script by hand.

    Args:
        pid_file: The ``--pid-file`` path, or ``None``.

    Returns:
        An existing, empty directory to record into.
    """
    if pid_file is None:
        return tempfile.mkdtemp(prefix="kindly-fixture-chain-")
    record_dir = pid_file + ".chain"
    os.makedirs(record_dir, exist_ok=True)
    return record_dir


def _read_chain(record_dir: str) -> list[dict[str, int]]:
    """Read whatever the descendant chain has recorded about itself so far.

    Args:
        record_dir: Directory the generations write into.

    Returns:
        The records read, ordered by generation. A file that is present but not
        yet readable is skipped rather than raised on -- it cannot happen, given
        the atomic rename each generation uses, and treating it as fatal here
        would turn a race that does not exist into a crash that does.
    """
    records: list[dict[str, int]] = []
    try:
        names = os.listdir(record_dir)
    except OSError:
        return records
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(record_dir, name), encoding="utf-8") as handle:
                records.append(json.load(handle))
        except (OSError, ValueError):
            continue
    records.sort(key=lambda entry: entry["generation"])
    return records


def _await_chain(
    record_dir: str, *, expected: int, timeout: float
) -> list[dict[str, int]] | None:
    """Wait for every generation of the chain to record itself.

    **The deadline is checked before the first read**, not after. That is what
    makes a bound of zero expire deterministically, which is how the expiry path
    is driven at all: a case that instead asked for a slow chain would be racing
    the machine it runs on.

    Args:
        record_dir: Directory the generations write into.
        expected: How many generations were asked for.
        timeout: Seconds to wait for all of them.

    Returns:
        The records, once there are ``expected`` of them, or ``None`` if the
        deadline passed first.
    """
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            return None
        records = _read_chain(record_dir)
        if len(records) >= expected:
            return records
        time.sleep(_CHAIN_POLL_SECONDS)


def _reap(pid: int) -> None:
    """Kill one process this script started, tolerating its having gone.

    Keyed on a pid this script spawned or was told about by a process it
    spawned, never on a name.

    Args:
        pid: The process to kill.
    """
    try:
        os.kill(pid, _REAP_SIGNAL)
    except OSError:
        return


def _spawn_grandchild(*, new_session: bool, record_dir: str, depth: int) -> int:
    """Start a descendant process and return its pid.

    Exists so a test can observe what happens to a *tree*: a parent that kills
    only its direct child leaves this process running, and that defect cannot be
    seen without a second generation.

    ``new_session`` selects which of the two topologies a real browser can
    present. A production Chromium is launched with ``start_new_session`` set on
    every POSIX platform (``nodriver_worker.py:608`` spells it
    ``start_new_session=(os.name == "posix")``), which makes it its own session
    and group leader and puts it outside any process group a reaper could aim at
    the worker; the default here inherits this process's group instead. A reaper
    has to survive both, and a single flag producing only one of them would let
    half a fix look complete. The
    argument is POSIX-only in ``subprocess`` and is ignored on Windows, which
    has no session to start -- so both settings describe the same topology
    there, and the Windows tree-walk this fixture serves keys on parentage
    rather than on groups anyway.

    Its standard output and standard error are ``DEVNULL``, deliberately. A
    descendant that inherited this process's pipes would hold them open after
    this process exits, so the parent's readers would never see end of file and
    every non-hanging case would hang. That is realistic browser behaviour, but
    it is a *different* claim from the orphan one, and a single flag producing
    both would make a failure ambiguous. A test that needs a pipe-holding
    descendant should get a second flag rather than a change to this one.

    ``new_session`` is applied to this **first** generation only; deeper ones
    inherit its session. That is production's topology rather than a
    simplification: Chromium calls ``setsid`` for itself and its renderers stay
    in the group it leads.

    Args:
        new_session: Whether the descendant calls ``setsid`` before it runs.
        record_dir: Directory every generation records itself into.
        depth: How many generations to build, including this one.

    Returns:
        The first generation's pid. The rest are deliberately not returned:
        the point of a chain is that something has to *walk* to them.
    """
    grandchild = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DESCENDANT_SOURCE,
            _DESCENDANT_SOURCE,
            record_dir,
            "1",
            str(depth),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )
    return grandchild.pid


def _write_pid_file(
    path: str,
    *,
    pid: int,
    grandchild_pid: int | None,
    chain: list[dict[str, int]],
) -> None:
    """Record this process's pid and its descendants', for a reader that has no frame.

    ``chain`` is written **unconditionally**, empty when no descendant was asked
    for. A key that appears only under some flag makes the file's shape a
    function of the command line, which is worse for its two readers than a key
    that is sometimes empty.

    Args:
        path: Where to write the record.
        pid: This process's own pid.
        grandchild_pid: The first generation's pid, or ``None`` when none was
            asked for. Kept beside ``chain`` rather than derived from it,
            because it is the announced half and the chain is the discoverable
            half, and a reader of one should not have to know about the other.
        chain: What each generation reported about itself, ordered.
    """
    # Written whole and then flushed, because the reader is told it may read the
    # instant readiness arrives: a partial line would be a decode error rather
    # than a wait, and no amount of retrying at the reader would be correct.
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"pid": pid, "grandchild_pid": grandchild_pid, "chain": chain}, handle
        )
        handle.flush()


def _hang(limit_seconds: float) -> None:
    """Block until the process is signalled, or until the backstop expires.

    Bounded rather than infinite. See :data:`MAX_LIFETIME_SECONDS` for why, and
    for why no test may treat the expiry as a synchronization point.

    Args:
        limit_seconds: Longest time to stay blocked.
    """
    deadline = time.monotonic() + limit_seconds
    while time.monotonic() < deadline:
        time.sleep(_SLEEP_SLICE_SECONDS)


def _report_incomplete_chain(
    record_dir: str, *, expected: int, grandchild_pid: int
) -> int:
    """Say that the chain never finished building, reap what can be named, and give up.

    Neither of the alternatives is acceptable. Announcing readiness anyway would
    hand a caller the half-built tree the wait exists to prevent, while this
    script's own documentation went on promising otherwise. Hanging would
    surface as the caller's readiness timeout, whose message names neither the
    chain nor how much of it arrived.

    **Two limits, stated rather than left to be found.** A generation that has
    started but not yet recorded itself is in neither list and survives to its
    own :data:`MAX_LIFETIME_SECONDS` backstop -- reachable only above depth one,
    which is why the case driving this path uses depth one. And the caller
    removes the record directory immediately after this returns, which can land
    while a generation is inside its own ``open``; that generation has already
    been signalled by then, and its traceback goes to ``DEVNULL``.

    Args:
        record_dir: Directory the generations were recording into.
        expected: How many generations were asked for.
        grandchild_pid: The first generation, which this process started and can
            therefore always name.

    Returns:
        :data:`CHAIN_INCOMPLETE_EXIT_CODE`, for the caller to exit with.
    """
    observed = _read_chain(record_dir)
    _emit_frame(
        CHAIN_INCOMPLETE_STAGE,
        "Descendant chain did not finish building",
        {"expected": expected, "observed": len(observed)},
    )
    # Deepest first, so a generation cannot be reparented out of reach between
    # one kill and the next; then the one this process started, which is the
    # only generation guaranteed to be nameable when no record arrived at all.
    for entry in reversed(observed):
        _reap(entry["pid"])
    _reap(grandchild_pid)
    return CHAIN_INCOMPLETE_EXIT_CODE


def main(argv: list[str]) -> int:
    """Run the script's fixed sequence against the flags it was given.

    Args:
        argv: Arguments after the program name.

    Returns:
        The process exit code.
    """
    args = _parse_args(argv)

    # The chain comes first, and is waited for, so that readiness means the
    # whole tree exists. Announced the other way round, a harness that reaps on
    # the readiness frame could sample the tree mid-construction and miss the
    # generations that had not started yet.
    grandchild_pid: int | None = None
    chain: list[dict[str, int]] = []
    if args.spawn_grandchild:
        record_dir = _chain_record_directory(args.pid_file)
        try:
            grandchild_pid = _spawn_grandchild(
                new_session=args.grandchild_new_session,
                record_dir=record_dir,
                depth=args.grandchild_depth,
            )
            awaited = _await_chain(
                record_dir,
                expected=args.grandchild_depth,
                timeout=args.chain_timeout,
            )
            if awaited is None:
                return _report_incomplete_chain(
                    record_dir,
                    expected=args.grandchild_depth,
                    grandchild_pid=grandchild_pid,
                )
            chain = awaited
        finally:
            # The records have been read into memory by now, and the directory
            # is this script's own temporary one, so leaving it behind would
            # leak a directory per run rather than preserve anything.
            shutil.rmtree(record_dir, ignore_errors=True)

    # Before the frame, not after: a reader is promised the file is there once
    # readiness has arrived, and that promise is what lets a *cancelled* parent
    # -- which never receives this script's frames -- still name what to reap.
    if args.pid_file:
        _write_pid_file(
            args.pid_file,
            pid=os.getpid(),
            grandchild_pid=grandchild_pid,
            chain=chain,
        )

    # Readiness before any other output, so a reader can consume exactly one
    # line to learn the script is up and to learn the pids it must reap.
    _emit_frame(
        READY_STAGE,
        "Fixture child ready",
        {"pid": os.getpid(), "grandchild_pid": grandchild_pid},
    )

    for stage in args.emit_frame:
        _emit_frame(stage, "Fixture child frame")

    if args.stderr_garbage:
        _write_stderr_garbage()

    # Through the binary buffer, so neither a Windows newline translation nor a
    # console codepage stands between the caller's text and the bytes the parent
    # reads as the rendered page.
    if args.stdout:
        sys.stdout.buffer.write(args.stdout.encode("utf-8"))
        sys.stdout.buffer.flush()

    # Generated here rather than passed in, because the payload this exists for
    # is larger than a Windows command line may be.
    if args.stdout_bytes:
        sys.stdout.buffer.write(
            bytes(stdout_pattern_byte(i) for i in range(args.stdout_bytes))
        )
        sys.stdout.buffer.flush()

    # Last, so that everything the caller asked for has already been written and
    # flushed by the time the process stops making progress.
    if args.hang:
        _hang(MAX_LIFETIME_SECONDS)

    return args.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
