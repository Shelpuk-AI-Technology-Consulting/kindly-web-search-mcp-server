"""A stand-in for the nodriver worker, driven entirely by its command line.

This script is **test-only** and is never imported. It is spawned by path, the
way the real worker is spawned by module, so that the parent-side subprocess
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

Its behaviours are **orthogonal flags**, not exclusive modes, because the
lifecycle tests need combinations: "emit frames and then hang" is how the claim
that a timed-out run still yields the frames received before the deadline gets
exercised. They are applied in a fixed order:

1. spawn a descendant, if asked, and learn its pid;
2. write the pid file, if asked;
3. emit the readiness frame on standard error and flush it;
4. emit each requested frame;
5. write the requested standard-error garbage;
6. write the requested standard-output payload;
7. hang, if asked, until signalled;
8. exit with the requested code.

The descendant is spawned **before** readiness is announced, so a harness that
reaps the process tree the moment it sees the readiness frame can never observe
a half-built tree. The pid file is written in that same window, and for a
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
import subprocess
import sys
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
    return parser.parse_args(argv)


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


def _spawn_grandchild(*, new_session: bool) -> int:
    """Start a descendant process and return its pid.

    Exists so a test can observe what happens to a *tree*: a parent that kills
    only its direct child leaves this process running, and that defect cannot be
    seen without a second generation.

    ``new_session`` selects which of the two topologies a real browser can
    present. A production Chromium is launched with ``start_new_session`` set on
    every POSIX platform (``nodriver_worker.py:608`` spells it
    ``start_new_session=(os.name == "posix")``), which makes it its own session
    and group leader and puts it outside any process group a reaper could aim at
    the worker; the default here inherits this process's group instead. A reaper has to survive both, and a single
    flag producing only one of them would let half a fix look complete. The
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

    Args:
        new_session: Whether the descendant calls ``setsid`` before it runs.

    Returns:
        The descendant's pid.
    """
    # `-c` rather than a second copy of this script: the descendant needs to do
    # nothing but stay alive, and giving it this script's flags would invite it
    # to grow behaviour nothing asked for. Its lifetime carries the same backstop
    # as the hang, because it is the process most likely to be left behind.
    snippet = (
        "import time\n"
        f"deadline = time.monotonic() + {MAX_LIFETIME_SECONDS}\n"
        "while time.monotonic() < deadline:\n"
        f"    time.sleep({_SLEEP_SLICE_SECONDS})\n"
    )
    grandchild = subprocess.Popen(
        [sys.executable, "-c", snippet],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )
    return grandchild.pid


def _write_pid_file(path: str, *, pid: int, grandchild_pid: int | None) -> None:
    """Record this process's pid and its descendant's, for a reader that has no frame.

    Args:
        path: Where to write the record.
        pid: This process's own pid.
        grandchild_pid: The descendant's pid, or ``None`` when none was asked for.
    """
    # Written whole and then flushed, because the reader is told it may read the
    # instant readiness arrives: a partial line would be a decode error rather
    # than a wait, and no amount of retrying at the reader would be correct.
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "grandchild_pid": grandchild_pid}, handle)
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


def main(argv: list[str]) -> int:
    """Run the script's fixed sequence against the flags it was given.

    Args:
        argv: Arguments after the program name.

    Returns:
        The process exit code.
    """
    args = _parse_args(argv)

    # The descendant comes first so that readiness means the whole tree exists.
    # Announced the other way round, a harness that reaps on the readiness frame
    # could sample the tree between the announcement and the spawn and miss it.
    grandchild_pid = (
        _spawn_grandchild(new_session=args.grandchild_new_session)
        if args.spawn_grandchild
        else None
    )

    # Before the frame, not after: a reader is promised the file is there once
    # readiness has arrived, and that promise is what lets a *cancelled* parent
    # -- which never receives this script's frames -- still name what to reap.
    if args.pid_file:
        _write_pid_file(
            args.pid_file, pid=os.getpid(), grandchild_pid=grandchild_pid
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

    # Last, so that everything the caller asked for has already been written and
    # flushed by the time the process stops making progress.
    if args.hang:
        _hang(MAX_LIFETIME_SECONDS)

    return args.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
