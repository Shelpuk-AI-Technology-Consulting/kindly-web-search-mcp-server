"""Tests for the extracted worker runner and for the boundary it defines.

Two groups, at deliberately different layers.

**The runner's own behaviour is asserted against a real child process.** That is
not a shortcut around writing a double — it is the whole reason
`scrape/worker_runner.py` exists as a separate module. Section 10.4 of
``.system_design/TEST_SUITE.md`` classifies each production file as hermetically
testable or not and exempts the second kind from the coverage gate, because a
process tree dying, a pipe draining and an exit status arriving cannot be
observed without a process. `.coveragerc-gate` names this module in its ``omit``
list. A hermetic seam bolted onto the runner to make it gateable would
contradict that classification, so these cases spawn an interpreter and are
marked ``subsystem``.

**The boundary itself is asserted hermetically.** The split is only worth
anything while it holds, and nothing about a file's contents needs a subprocess
to check. Those cases are unmarked and run in the fast lane.

The larger lifecycle battery — timeout and kill, heartbeat cadence, frame
fragmentation across chunk boundaries, orphaned children — belongs to the
subsystem step that owns a purpose-built fixture child. What is here is what
this extraction itself must prove: that the runner takes an arbitrary command,
that it still reads a child's streams, and that the two modules did not grow
back together.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# This directory too: the suite is not an installed package, and the L3 cases
# below import two platform-specific pid helpers from a sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# `test_worker_child_fixture` is imported rather than copied from. `_pid_is_alive`
# and `_kill_pid` are forty lines of platform-specific probing that already
# exist, already run on both platforms, and already carry the reasoning for why
# a signal-0 probe is not enough on Linux. Generalising them into a shared
# harness belongs to the anti-flake harness step; taking a second committed
# consumer now is what tells that step the helpers are load-bearing rather than
# local to the module that introduced them.
from test_worker_child_fixture import FIXTURE_CHILD, _kill_pid, _pid_is_alive

from kindly_web_search_mcp_server.scrape import universal_html, worker_runner
from kindly_web_search_mcp_server.scrape.worker_runner import _run_worker_command

#: The markup the fixture child writes. Byte-identical to
#: `tests/test_universal_html_loader.py`'s `WORKER_STDOUT`: that file asserts the
#: loader hands this payload back when the runner is doubled, and this one
#: asserts the runner produces it from a real pipe. Same payload, two strengths.
WORKER_STDOUT = "<html><body><p>ok</p></body></html>"

#: Longer than any child here needs and far shorter than a stuck test should
#: run. The runner clamps to at least 1.0s, so a smaller number would not mean
#: what it says.
GENEROUS_TIMEOUT_SECONDS = 30.0


def _child_environment() -> dict[str, str]:
    """Build the environment for a fixture child.

    Inherits the parent's environment rather than starting from empty. An empty
    environment is not the more hermetic choice on every platform: Windows needs
    ``SYSTEMROOT`` to start a process at all, and these cases are portable.

    Returns:
        A copy of the current environment, safe to hand to a child.
    """
    return dict(os.environ)


@pytest.fixture(autouse=True)
def _no_ambient_timeout_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the timeout override every case in this module would otherwise read.

    Still load-bearing after the runner started resolving
    ``KINDLY_HTML_TOTAL_TIMEOUT_SECONDS`` from its `env` argument rather than
    from `os.environ`, but for a different reason than it was written for:
    `_child_environment` copies the process environment, so an exported value
    lands in `env` and reaches the runner by the front door. A shell exporting a
    small one would turn every case here red for a reason no assertion names; a
    large one would make the budget assertion vacuous.

    Args:
        monkeypatch: pytest's environment patcher, which restores on teardown.
    """
    monkeypatch.delenv("KINDLY_HTML_TOTAL_TIMEOUT_SECONDS", raising=False)


@pytest.mark.subsystem
async def test_runs_an_arbitrary_command_and_returns_its_stdout() -> None:
    """Execute whatever argv it is given, and hand back what that process wrote

    Two claims in one case, because a single real child proves both and two
    children would prove neither more strongly.

    The first is the seam this extraction exists to create: the runner accepts a
    command it did not build. Nothing here names the worker module, so a runner
    that had quietly kept a hard-coded command line could not pass.

    The second is the claim the loader tests gave up when they stopped driving a
    process double — that the parent actually reads the child's stdout. It is
    asserted on the payload the child really wrote through a real pipe, where no
    fake can satisfy it, and the comparison is whole-string rather than by
    substring so truncation or a mangled decode fails too.
    """
    # Written through `sys.stdout.buffer`, not `sys.stdout`. Text mode
    # translates "\n" to "\r\n" on Windows, and this case compares the payload
    # whole; the current literal happens to contain no newline, which is exactly
    # the kind of accident that turns into a Windows-only failure the first time
    # someone edits it.
    payload = f"import sys; sys.stdout.buffer.write({WORKER_STDOUT.encode()!r})"

    html = await _run_worker_command(
        [sys.executable, "-c", payload],
        env=_child_environment(),
        default_timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
        diagnostics=None,
    )

    assert html == WORKER_STDOUT


@pytest.mark.subsystem
async def test_non_zero_exit_raises_carrying_the_child_stderr() -> None:
    """Surface a failed child as a readable error naming its code and its output

    The stderr chain — chunked read, line splitting, frame filtering, tail
    accumulation — moved to the new module as a block. This is the case that
    fails if any link in it was left behind: the message can only carry the
    child's complaint if every step ran.
    """
    payload = (
        "import sys; sys.stderr.write('worker exploded\\n'); sys.stderr.flush(); "
        "sys.exit(3)"
    )

    with pytest.raises(RuntimeError) as excinfo:
        await _run_worker_command(
            [sys.executable, "-c", payload],
            env=_child_environment(),
            default_timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
            diagnostics=None,
        )

    message = str(excinfo.value)
    assert "exit=3" in message
    assert "worker exploded" in message


def _imported_root_modules(path: Path) -> set[str]:
    """Collect the top-level module names a source file imports.

    Reads the file's AST rather than its text, so a module named in a docstring,
    a comment or a string literal is not mistaken for an import. Relative imports
    are ignored: they can only name modules inside this package.

    Args:
        path: The source file to scan.

    Returns:
        The root name of every absolute import in the file, for example
        ``asyncio`` for both ``import asyncio.subprocess`` and
        ``from asyncio import wait_for``.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_universal_html_manages_no_subprocess() -> None:
    """Keep every process primitive out of the gated module

    The verify condition for the extraction, expressed as the only thing that
    can actually be checked cheaply and forever: `universal_html.py` imports
    neither `asyncio` nor `subprocess`. Both are required to start, wait on,
    stream from or kill a process, so their joint absence is not a proxy for
    "no subprocess management" — it is the thing itself.

    Asserted on imports rather than on a list of forbidden call expressions
    because the list can never be complete, and because this form fails on the
    *first* line of a regression rather than on its tenth.

    It has a second effect worth naming. `universal_html.asyncio` is exactly
    what four tests used to patch to replace the spawn primitive process-wide;
    with the import gone, that target raises `AttributeError` instead of
    quietly working, so the coupling the suite design ruled out cannot come
    back by habit.
    """
    imported = _imported_root_modules(Path(universal_html.__file__))

    assert "asyncio" not in imported, (
        "universal_html.py imports asyncio again — the parent-side process "
        "management belongs in worker_runner.py"
    )
    assert "subprocess" not in imported, (
        "universal_html.py imports subprocess again — see worker_runner.py"
    )


def test_universal_html_keeps_the_hermetically_testable_half() -> None:
    """Hold the other side of the same boundary

    The absence check above is satisfied by an empty file. This is what stops
    the split being read as "move everything": the pure command builder and the
    Markdown-suffix probe path stay where the coverage gate can see them, which
    is the entire reason the boundary was drawn in this place rather than around
    the whole feature.
    """
    assert callable(universal_html._build_worker_command)
    assert callable(universal_html._probe_markdown_suffix)
    assert callable(universal_html.fetch_html_via_nodriver)


def test_worker_runner_does_not_from_import_the_spawn_primitive() -> None:
    """Reach the spawn through the module object, never through a bound name

    `from asyncio import create_subprocess_exec` resolves the callable once, at
    import time, and binds it into this module. `asyncio.create_subprocess_exec`
    resolves it on every call, through the shared module object — which is what
    lets a test replace it and what a from-import silently defeats.

    This is a live trap and not a hypothetical: the characterization tests that
    guarded this extraction depended on that mechanism while it was in progress,
    and the failure it produces (assertions failing in tests that name neither
    this module nor the import, plus real browsers launched from a lane that
    must not start one) points nowhere near its cause.
    """
    tree = ast.parse(Path(worker_runner.__file__).read_text(encoding="utf-8"))
    offenders = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "asyncio"
        for alias in node.names
    ]

    assert not offenders, (
        f"worker_runner.py binds {offenders} at import time; call them through "
        f"the asyncio module object instead"
    )


# Everything the extraction moved out of `universal_html.py`. A list, not a spot
# check: the move is only worth its diff if the whole block travelled. Shared by
# the two cases below so the set has one definition -- a second enumeration is
# the drift this list exists to prevent.
PROCESS_MANAGEMENT_SURFACE = (
    "_run_worker_command",
    "_run_pipe_probe",
    "_read_probe_stream",
    "_read_stdout_stream",
    "_read_stderr_stream",
    "_consume_stderr_line",
    "_finalize_stderr_state",
    "_append_tail_text",
    "_maybe_emit_stream_progress",
    "_emit_worker_heartbeat",
    "_terminate_process_tree",
    # The tree walk `_terminate_process_tree` was changed to perform. Listed
    # here for the same reason as the rest: the docstring rule and the
    # ownership case both read this tuple, so a helper left out of it is a
    # helper neither reaches.
    "_parse_parent_pid",
    "_read_parent_map",
    "_collect_descendants",
    "_signal_descendants",
    "_subprocess_launch_options",
    "_StdoutAccumulator",
    "_StderrAccumulator",
)


def test_worker_runner_owns_the_process_management_surface() -> None:
    """Name what moved, so a partial move back is a failure rather than a drift

    A list, not a spot check. The extraction is only worth its diff if the whole
    block travelled: leaving one stream reader or the terminator behind puts
    unhermetic code back inside the gated module without any single assertion
    above noticing.
    """
    missing = [
        name for name in PROCESS_MANAGEMENT_SURFACE if not hasattr(worker_runner, name)
    ]
    assert not missing, f"worker_runner.py is missing {missing}"

    strays = [
        name
        for name in PROCESS_MANAGEMENT_SURFACE
        if getattr(getattr(universal_html, name, None), "__module__", None)
        == universal_html.__name__
    ]
    assert not strays, f"{strays} were defined again in universal_html.py"


def test_run_worker_command_takes_the_command_positionally() -> None:
    """Keep the argv the first positional parameter, and everything else keyword

    The loader tests read the command out of `call.args[0]`, and the subsystem
    cases above pass it positionally. Making it keyword-only would leave both
    silently reading an empty tuple rather than failing, so the shape is pinned
    where a change to it has to be deliberate.
    """
    import inspect

    parameters = list(inspect.signature(_run_worker_command).parameters.values())

    assert parameters[0].name == "command"
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters[1:]
    )


def test_asyncio_is_not_reachable_through_universal_html() -> None:
    """Prove the removed patch target is really gone

    The four loader cases were rewritten off
    `universal_html.asyncio.create_subprocess_exec`. If the attribute came back
    — by a stray `import asyncio` that the import guard somehow allowed, or by
    an assignment — the old target would resolve again and the next author
    reaching for it would find it working. One line, so that cannot happen
    quietly.
    """
    assert not hasattr(universal_html, "asyncio")


@pytest.mark.subsystem
async def test_timeout_budget_is_read_from_the_environment_it_is_given() -> None:
    """Resolve the budget from the `env` argument, never from the ambient one

    The runner takes the environment it hands the child as a parameter, and the
    override is read from that same mapping. It could have been read from
    `os.environ` — at the one production call site the two are equal, because
    the loader builds `env` from `os.environ` and never writes this key — but
    then the function would have an input its signature does not name, and the
    subsystem step that drives it against a fixture child would set the variable
    in `env` and watch the parent's value win, silently.

    The case is arranged so the two mappings **disagree**: the fixture above
    clears the variable from the process environment, and only `env` carries it.
    A runner reading `os.environ` resolves the default instead and the assertion
    fails.
    """
    from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

    diagnostics = Diagnostics(request_id="budget", enabled=True, stream=io.StringIO())
    env = _child_environment()
    env["KINDLY_HTML_TOTAL_TIMEOUT_SECONDS"] = "17"

    await _run_worker_command(
        [sys.executable, "-c", "pass"],
        env=env,
        default_timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
        diagnostics=diagnostics,
    )

    budget = next(
        entry
        for entry in diagnostics.entries
        if entry["stage"] == "worker.timeout_budget_parent"
    )
    assert budget["data"]["effective_timeout_seconds"] == 17.0
    assert budget["data"]["used_default"] is False


@pytest.mark.subsystem
async def test_runner_diagnostics_keep_their_order() -> None:
    """Pin where the runner's own records sit relative to the process it starts

    The extraction kept the timeout parse inside the runner, rather than
    resolving the budget in the caller, on the stated ground that moving it
    would move `worker.timeout_budget_parent` ahead of the spawn. Nothing in the
    suite checked that, so the justification could quietly stop being true. This
    is the runner's half of the check; the loader's half is
    `test_caller_side_diagnostics_keep_their_order` in
    `tests/test_universal_html_loader.py`.

    Order, not membership — all three records appear on every successful run, so
    a set comparison would pass with them shuffled.
    """
    from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

    diagnostics = Diagnostics(request_id="order", enabled=True, stream=io.StringIO())
    payload = f"import sys; sys.stdout.buffer.write({WORKER_STDOUT.encode()!r})"

    await _run_worker_command(
        [sys.executable, "-c", payload],
        env=_child_environment(),
        default_timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
        diagnostics=diagnostics,
    )

    stages = [entry["stage"] for entry in diagnostics.entries]
    pinned = [
        stage
        for stage in stages
        if stage
        in {"worker.process_started", "worker.timeout_budget_parent", "worker.stdout"}
    ]
    assert pinned == [
        "worker.process_started",
        "worker.timeout_budget_parent",
        "worker.stdout",
    ], stages


# --------------------------------------------------------------------------
# Platform guards, and why the two are spelled differently
# --------------------------------------------------------------------------

# The rule, so a future reader can apply it without re-measuring:
#
#   Use `sys.platform` where the branch touches stdlib that exists only on that
#   platform AND a second mypy run under `--platform win32` covers it.
#   Use `os.name` where you want the branch checked on EVERY run.
#
# mypy narrows on `sys.platform` and never on `os.name`, and it does not
# type-check code it considers unreachable. So the spelling decides which runs
# read the branch at all, and the two functions below want different answers.
#: Ceiling on waiting for the fixture child to announce itself by writing its
#: pid file. Generous on purpose: it bounds a hang, it is not a measurement of
#: how fast an interpreter starts, and a tight value here is the flake generator
#: section 5.2a warns about.
PID_FILE_TIMEOUT_SECONDS = 30.0

#: Ceiling on waiting for a signalled process to actually be gone. `SIGKILL`
#: delivery is not instantaneous and neither is `taskkill`, so the claim "no
#: descendant survives" is polled to this deadline rather than sampled once --
#: a single probe taken too early answers "alive" truthfully and fails a correct
#: fix, and one taken too late would pass a broken one only by luck.
DEATH_TIMEOUT_SECONDS = 15.0

#: Budget handed to the runner by the cases that want it to expire. Long enough
#: that the child has certainly written its pid file first (which those cases
#: also wait for), short enough not to dominate the module's runtime. The runner
#: clamps below 1.0s, so nothing smaller would mean what it says.
EXPIRING_TIMEOUT_SECONDS = 3.0

#: One polling slice. Every wait in this module is a condition with a deadline,
#: never a bare sleep sized to "long enough".
POLL_SLICE_SECONDS = 0.05


async def _await_pid_file(pid_file: Path) -> dict[str, Any]:
    """Wait until the fixture child has recorded its pids, then read them.

    This is the readiness signal for every case below, and it has to be: the
    child's readiness *frame* travels on stderr, and `_run_worker_command`
    appends worker frames to the caller's diagnostics on the timeout path and
    not at all on the cancellation path -- so mid-run there is nothing else to
    wait for. Cancelling before the file exists would tear down a tree that had
    not been built and prove nothing.

    Args:
        pid_file: Path the child was told to write.

    Returns:
        The decoded record, carrying ``pid`` and ``grandchild_pid``.
    """
    deadline = time.monotonic() + PID_FILE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            return dict(json.loads(pid_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            await asyncio.sleep(POLL_SLICE_SECONDS)
    raise AssertionError(
        f"the fixture child never wrote {pid_file} within "
        f"{PID_FILE_TIMEOUT_SECONDS}s; it is the only mid-run channel these "
        "cases have, so nothing below can be trusted without it."
    )


async def _wait_until_gone(pid: int) -> bool:
    """Report whether a pid stops being a running process before the deadline.

    Args:
        pid: The process to watch.

    Returns:
        ``True`` if it is gone, ``False`` if it outlived
        :data:`DEATH_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + DEATH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        await asyncio.sleep(POLL_SLICE_SECONDS)
    return False


@contextlib.asynccontextmanager
async def _worker_running(
    pid_file: Path, *flags: str, timeout_seconds: float
) -> AsyncIterator[tuple[asyncio.Task[str], dict[str, Any]]]:
    """Start the fixture child through the real runner and wait for its tree.

    Drives `_run_worker_command` rather than spawning directly, because the
    claim under test is what *the runner* does to a process tree. The child is
    always given ``--hang``: every case here is about a worker that is still
    running when the parent gives up on it.

    Teardown reaps by pid whatever the case left behind, so a failing assertion
    costs one red case and not a stray browser stand-in per run.

    Args:
        pid_file: Where the child records its pids.
        *flags: Extra fixture-child flags, before ``--hang``.
        timeout_seconds: Budget handed to the runner.

    Yields:
        The running task, and the child's recorded pids.
    """
    command = [
        sys.executable,
        str(FIXTURE_CHILD),
        "--pid-file",
        str(pid_file),
        "--spawn-grandchild",
        *flags,
        "--hang",
    ]
    task: asyncio.Task[str] = asyncio.create_task(
        _run_worker_command(
            command,
            env=_child_environment(),
            default_timeout_seconds=timeout_seconds,
            diagnostics=None,
        )
    )
    recorded: dict[str, Any] = {}
    try:
        recorded = await _await_pid_file(pid_file)
        yield task, recorded
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await task
        # Descendant first: reaped the other way round it is reparented and this
        # loop would be signalling a pid nothing owns any more.
        for pid in (recorded.get("grandchild_pid"), recorded.get("pid")):
            if isinstance(pid, int):
                _kill_pid(pid)


@pytest.mark.subsystem
async def test_a_cancelled_run_leaves_no_detached_descendant(tmp_path: Path) -> None:
    """Reap a descendant that put itself in its own session, on cancellation

    This is the shipped defect, in the shape production presents it. The server
    wraps every fetch in `asyncio.wait_for`, which **cancels** on expiry, and a
    client disconnecting does the same -- so this path, not the timeout one, is
    the common way a worker dies. Its descendant is a browser that called
    `setsid` for itself at launch, which is what makes a process-group kill
    aimed at the worker miss it entirely.

    Measured against the shipped function: the grandchild is alive afterwards,
    reparented to pid 1.

    Args:
        tmp_path: Per-test directory for the pid file.
    """
    pid_file = tmp_path / "pids.json"
    async with _worker_running(
        pid_file, "--grandchild-new-session", timeout_seconds=30.0
    ) as (task, recorded):
        grandchild = recorded["grandchild_pid"]
        assert _pid_is_alive(grandchild)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _wait_until_gone(grandchild), (
            f"the browser stand-in {grandchild} outlived the worker's "
            "cancellation. That is the leak: it is now reparented and nothing "
            "will ever reap it."
        )


@pytest.mark.subsystem
async def test_a_timed_out_run_leaves_no_detached_descendant(tmp_path: Path) -> None:
    """Reap the same descendant when the budget expires rather than the caller

    A separate case rather than a parametrization of the one above, because the
    two arrive through **different `except` blocks** in `_run_worker_command`
    and a fix applied to one of them is a real and easy mistake. Nothing else in
    this module would notice.

    Args:
        tmp_path: Per-test directory for the pid file.
    """
    pid_file = tmp_path / "pids.json"
    async with _worker_running(
        pid_file,
        "--grandchild-new-session",
        timeout_seconds=EXPIRING_TIMEOUT_SECONDS,
    ) as (task, recorded):
        grandchild = recorded["grandchild_pid"]

        with pytest.raises(asyncio.TimeoutError):
            await task

        assert await _wait_until_gone(grandchild), (
            f"the browser stand-in {grandchild} outlived the worker's timeout."
        )


@pytest.mark.subsystem
async def test_a_cancelled_run_reaps_a_descendant_in_the_callers_own_group(
    tmp_path: Path,
) -> None:
    """Reap a descendant the group pass is required to skip

    The worker is spawned with no `start_new_session`, so it sits in the
    server's process group and so does any descendant that did not detach. The
    group pass **must** skip that group -- signalling it would kill the server
    -- which leaves this descendant reachable only by the per-pid pass.

    So this case is what makes the per-pid pass load-bearing: delete it and a
    fix that reaps only by group still passes the two cases above, because their
    descendant leads a group of its own.

    Args:
        tmp_path: Per-test directory for the pid file.
    """
    pid_file = tmp_path / "pids.json"
    async with _worker_running(pid_file, timeout_seconds=30.0) as (task, recorded):
        grandchild = recorded["grandchild_pid"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _wait_until_gone(grandchild), (
            f"the same-group descendant {grandchild} survived. A group kill "
            "cannot reach it -- its group is the caller's -- so this is the "
            "per-pid pass failing."
        )


@pytest.mark.subsystem
async def test_a_cancelled_run_leaves_a_process_it_did_not_start_alone(
    tmp_path: Path,
) -> None:
    """Leave the pool's browser running, which is a claim about topology

    A pooled Chromium is spawned by the **server**, through
    `chromium_pool.ChromiumSlot._start`, not by the worker -- so it is the
    worker's sibling rather than its descendant. The control below is started
    exactly that way. Nothing special protects it: it survives because a walk
    rooted at the worker never reaches it, which is why the pooled and unpooled
    paths need no asymmetry in the terminator at all.

    The case earns its place by killing two mutations the descendant cases
    cannot see: reaping by process *name*, and walking from the wrong root.

    Args:
        tmp_path: Per-test directory for the pid file.
    """
    control = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(300)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        # Spawned exactly the way `_launch_chromium` spawns a browser, down to
        # the primitive: the control is only worth anything if it is the same
        # shape of process as the thing it stands in for.
        start_new_session=(os.name == "posix"),
    )
    try:
        pid_file = tmp_path / "pids.json"
        async with _worker_running(
            pid_file, "--grandchild-new-session", timeout_seconds=30.0
        ) as (task, _recorded):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Probed after the descendants are gone rather than immediately, so
            # "survived" means survived the whole reap and not merely that the
            # reap had not reached it yet.
            assert _pid_is_alive(control.pid), (
                f"the pooled-browser control {control.pid} was killed. It is "
                "not a descendant of the worker, so whatever reached it would "
                "reach every other browser this host is running."
            )
    finally:
        _kill_pid(control.pid)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(control.wait(), timeout=5.0)


#: A `/proc/<pid>/stat` line whose `comm` field is as hostile as the kernel
#: permits: it contains spaces *and* a closing bracket. Real programs do this --
#: a thread renamed at run time, or a binary whose name a user chose. The fields
#: after `comm` are `state ppid pgrp ...`, so the parent pid here is 4242.
HOSTILE_STAT_LINE = "1234 (weird ) name) S 4242 1234 1234 0 -1 4194304 100 0"

#: Signal the kill-loop cases hand to the doubles. Not `signal.SIGKILL`: that
#: name does not exist on Windows, and these cases are pure -- they must collect
#: and run there too. Production's choice of `SIGKILL` is made at the one call
#: site, inside the platform-guarded branch.
SENTINEL_SIGNAL = -12345


def test_the_parent_map_walk_collects_every_generation() -> None:
    """Collect grandchildren, not only children

    The whole defect this module is being changed for is a kill that stops at
    the first generation. A walk that did the same would reproduce it exactly
    one level deeper: Chromium is the worker's child, but Chromium's renderers
    are its grandchildren, and a browser left with live renderers is still a
    leak. Asserting a two-level tree is what separates "walks" from "lists
    children".
    """
    parent_map = {2: 1, 3: 2, 4: 3, 5: 1, 6: 4}

    assert sorted(worker_runner._collect_descendants(parent_map, 2)) == [3, 4, 6]


def test_the_parent_map_walk_ignores_processes_outside_the_subtree() -> None:
    """Collect the worker's tree and nothing else

    This is the pooled browser's whole protection. A pooled Chromium is spawned
    by the server, not by the worker, so it is a *sibling* of the worker rather
    than a descendant -- and the only thing keeping it alive through a cancelled
    fetch is that the walk does not reach it. Pid 5 below stands in for it.
    """
    parent_map = {2: 1, 3: 2, 5: 1}

    assert worker_runner._collect_descendants(parent_map, 2) == [3]


def test_the_parent_map_walk_terminates_on_a_cycle() -> None:
    """Terminate on a malformed map rather than spinning inside a kill path

    A real `/proc` cannot present a cycle, but this walk does not read a real
    `/proc` -- it reads a snapshot assembled from many files that were not read
    atomically, while pids are being recycled. A hang here would be the worst
    possible failure: it happens inside the cancellation handler, so the caller
    is already unwinding and the process would stop making progress with no
    error anywhere. Bounded by construction, and asserted rather than assumed.
    """
    parent_map = {2: 3, 3: 2, 4: 2}

    assert sorted(worker_runner._collect_descendants(parent_map, 2)) == [3, 4]


def test_the_parent_map_walk_is_empty_for_a_leaf() -> None:
    """Return nothing for a worker that started nothing

    The common case in production is a worker killed before it launched a
    browser. It must produce an empty list, not a `KeyError` -- the caller is
    already handling a timeout and an exception here would replace it.
    """
    assert worker_runner._collect_descendants({2: 1, 3: 1}, 2) == []


def test_the_stat_parse_survives_a_command_name_with_spaces_and_a_bracket() -> None:
    """Read the parent pid after the `comm` field, not after the second space

    `/proc/<pid>/stat`'s second field is the executable name in brackets, and it
    may itself contain spaces and a closing bracket. A `split()` on whitespace
    therefore reads a field that is not the parent pid -- and the number it
    returns is a plausible small integer, not an error. That number would go
    on to be treated as a process this code is entitled to `SIGKILL`, and one
    of its process group.

    The parse takes everything after the **last** bracket for that reason. A
    naive parse reads `4194304` from the line below; this one reads 4242.
    """
    assert worker_runner._parse_parent_pid(HOSTILE_STAT_LINE) == 4242


@pytest.mark.parametrize(
    "line",
    ["", "1234", "1234 (sh)", "1234 (sh) S", "not a stat line at all"],
    ids=["empty", "pid-only", "no-state", "no-ppid", "garbage"],
)
def test_the_stat_parse_returns_none_rather_than_raising(line: str) -> None:
    """Answer "unknown" for a line that is not one, in a path that must not raise

    Every one of these is reachable: a process exits between the directory
    listing and the read, and the kernel can hand back a truncated line. The
    walk drops what it cannot parse and keeps going, because one unreadable
    entry must not cost the caller the whole kill.

    Args:
        line: A stat line that carries no usable parent pid.
    """
    assert worker_runner._parse_parent_pid(line) is None


class _RecordingSignals:
    """Stand-in for `os.kill`/`os.killpg`/`os.getpgid`, recording what it was asked.

    Exists so the kill loop can be driven with no process anywhere. The loop is
    the one piece of this fix that cannot be tested against real processes
    without risking the test session itself -- see
    :func:`test_the_kill_loop_never_signals_the_callers_own_group`.
    """

    def __init__(self, groups: dict[int, int]) -> None:
        """Record the pid-to-group mapping this double will report.

        Args:
            groups: Group each pid belongs to.
        """
        self.groups = groups
        self.killed_groups: list[int] = []
        self.killed_pids: list[int] = []
        self.signal_numbers: list[int] = []

    def getpgid(self, pid: int) -> int:
        """Report the group of ``pid``.

        Args:
            pid: The process to look up.

        Returns:
            Its process group.

        Raises:
            ProcessLookupError: If the pid is not in the recorded mapping,
                matching what the real call does for a process that has exited.
        """
        if pid not in self.groups:
            raise ProcessLookupError(pid)
        return self.groups[pid]

    def killpg(self, group: int, signal_number: int) -> None:
        """Record a group signal.

        Args:
            group: The process group signalled.
            signal_number: The signal sent, recorded so one case can prove the
                loop forwards what it was given rather than choosing its own.
        """
        self.killed_groups.append(group)
        self.signal_numbers.append(signal_number)

    def kill(self, pid: int, signal_number: int) -> None:
        """Record a process signal.

        Args:
            pid: The process signalled.
            signal_number: The signal.
        """
        self.killed_pids.append(pid)
        self.signal_numbers.append(signal_number)


def test_the_kill_loop_signals_every_descendant_by_pid() -> None:
    """Signal each descendant individually, not only its group

    The per-pid pass is the general claim and the group pass is an optimisation
    over it. That ordering matters because the reverse is not true: a descendant
    whose group is excluded -- which is every descendant that did not `setsid`,
    since the worker shares the server's process group -- is reached by nothing
    else. Deleting this pass leaves those alive and passes every group-based
    assertion.
    """
    signals = _RecordingSignals({11: 11, 12: 11})

    worker_runner._signal_descendants(
        [11, 12],
        own_pid=99,
        own_group=99,
        signal_number=SENTINEL_SIGNAL,
        getpgid=signals.getpgid,
        killpg=signals.killpg,
        kill=signals.kill,
    )

    assert signals.killed_pids == [11, 12]
    assert signals.killed_groups == [11]
    # Forwarded, not chosen here: the signal is production's decision and this
    # loop must not quietly substitute a gentler one.
    assert set(signals.signal_numbers) == {SENTINEL_SIGNAL}


def test_the_kill_loop_never_signals_the_callers_own_group() -> None:
    """Refuse the one signal that would kill the server

    Not defensive. The worker is spawned with no `start_new_session`
    (`_subprocess_launch_options` returns `{}` off Windows), so it shares the
    **server's** process group, and so does every descendant that did not
    `setsid` for itself. Without this exclusion the loop reaches
    `os.killpg(<the server's own group>, SIGKILL)` on an ordinary cancelled
    fetch and takes the MCP server down with the browser.

    **The corresponding mutation must not be run against the real suite.**
    Deleting the exclusion and running any case that builds a same-group
    descendant signals pytest's own group: the session dies with no report and
    no traceback, which is indistinguishable from a crash. That is why the claim
    is pinned here, against a double, and why this case is green before any case
    in this module signals a real process.
    """
    signals = _RecordingSignals({11: 99, 12: 0, 13: 13})

    worker_runner._signal_descendants(
        [11, 12, 13],
        own_pid=99,
        own_group=99,
        signal_number=SENTINEL_SIGNAL,
        getpgid=signals.getpgid,
        killpg=signals.killpg,
        kill=signals.kill,
    )

    assert signals.killed_groups == [13]
    # Excluded from the *group* pass and still reaped individually: the
    # exclusion narrows which signal reaches them, never whether one does.
    assert signals.killed_pids == [11, 12, 13]


def test_the_kill_loop_never_signals_the_caller_or_init() -> None:
    """Refuse a pid the walk should never have produced

    A walk rooted at the worker cannot reach this process or pid 1 -- unless the
    snapshot it was built from contained a cycle or a recycled pid, both of
    which are admitted possibilities because the many `/proc` files behind it
    are not read atomically. The filter costs one comparison; being wrong costs
    the server or `init`.
    """
    signals = _RecordingSignals({1: 1, 99: 99, 13: 13})

    worker_runner._signal_descendants(
        [1, 99, 13],
        own_pid=99,
        own_group=99,
        signal_number=SENTINEL_SIGNAL,
        getpgid=signals.getpgid,
        killpg=signals.killpg,
        kill=signals.kill,
    )

    assert signals.killed_pids == [13]
    assert 1 not in signals.killed_groups and 99 not in signals.killed_groups


def test_the_kill_loop_survives_a_descendant_that_has_already_exited() -> None:
    """Keep going when a pid disappears mid-loop, which is the ordinary case

    Between the walk and the signal the worker is being torn down, so its
    children are exiting on their own. `getpgid` and `kill` both raise
    `ProcessLookupError` for a pid that has gone. Letting either escape would
    abandon every descendant after it in the list -- the browser could be the
    one abandoned, which is the whole leak.
    """
    signals = _RecordingSignals({13: 13})

    worker_runner._signal_descendants(
        [404, 13],
        own_pid=99,
        own_group=99,
        signal_number=SENTINEL_SIGNAL,
        getpgid=signals.getpgid,
        killpg=signals.killpg,
        kill=signals.kill,
    )

    assert signals.killed_groups == [13]
    assert 13 in signals.killed_pids


PLATFORM_GUARDS = {
    # Touches `subprocess.STARTUPINFO`, which typeshed declares win32-only. Under
    # `os.name` this produced `Module has no attribute "STARTUPINFO"` the moment
    # the module joined the type-check target.
    "_subprocess_launch_options": ("sys.platform", "os.name"),
    # Was `os.name`, and why it was is kept here rather than deleted, because it
    # was correct until the function changed: it touched no platform-exclusive
    # stdlib, so `os.name` left the `taskkill` path readable on the native run
    # as well as the win32 one — measured with an injected error, reported under
    # `os.name` and not under `sys.platform`.
    #
    # The tree walk ended that. `os.killpg`, `os.getpgid` and `signal.SIGKILL`
    # are POSIX-only in typeshed and mypy narrows on `sys.platform` alone, so
    # under `os.name` the POSIX branch reports three `attr-defined` errors — the
    # exact mirror of the `STARTUPINFO` defect above. The price is the one that
    # comment named: the Windows body is now unreachable natively and is read
    # only by the `--platform win32` invocation, which is what makes that
    # invocation load-bearing rather than belt-and-braces.
    "_terminate_process_tree": ("sys.platform", "os.name"),
}


@pytest.mark.parametrize(("function_name", "guards"), sorted(PLATFORM_GUARDS.items()))
def test_each_platform_guard_uses_the_spelling_its_checking_needs(
    function_name: str, guards: tuple[str, str]
) -> None:
    """Pin both halves of the two-spelling rule, so a tidy-up cannot erase coverage

    The asymmetry reads as an oversight and is not. Asserting only the
    `sys.platform` half would leave a "make it consistent" edit free to convert
    the other function and silently stop Linux from checking its Windows branch;
    asserting only the `os.name` half would let the `STARTUPINFO` defect return.
    Both directions are pinned because each fails a different way.

    Args:
        function_name: The guarded function in `worker_runner.py`.
        guards: The spelling it must use, and the one it must not.
    """
    required, forbidden = guards
    source = Path(worker_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=worker_runner.__file__)
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    assert function is not None, f"{function_name} has moved or been renamed."

    # Attribute accesses, not a text search: the comment that explains why the
    # rejected spelling was rejected has to be free to name it.
    accessed = {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }

    assert required in accessed, (
        f"{function_name} no longer reads `{required}`. See PLATFORM_GUARDS "
        "above for which runs each spelling leaves the branch checked by."
    )
    assert forbidden not in accessed, (
        f"{function_name} reads `{forbidden}`, which is the wrong spelling for "
        "it. See PLATFORM_GUARDS above."
    )


#: The two ways a Windows-only branch may be spelled in the runner, as
#: (module, attribute, expected value). Both are legitimate and the choice is
#: made per function -- see `PLATFORM_GUARDS` -- so a helper that locates such a
#: branch must accept either or it breaks the moment a guard is converted for a
#: reason `PLATFORM_GUARDS` records as correct.
WINDOWS_BRANCH_SPELLINGS = (("os", "name", "nt"), ("sys", "platform", "win32"))


def _windows_branch_of(function_name: str) -> ast.If:
    """Locate the Windows-only branch inside a function in the runner.

    Accepts either platform spelling. The **value** is pinned as well as the
    attribute: `os.name == "posix"` and `sys.platform == "darwin"` are also
    `Compare` nodes against the same attributes, and returning one of those as
    "the Windows branch" would leave every assertion built on it passing while
    reading the wrong code.

    Args:
        function_name: The function to search.

    Returns:
        The ``if`` node guarding that function's Windows-only body.
    """
    source = Path(worker_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=worker_runner.__file__)
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    assert function is not None, f"{function_name} has moved or been renamed."

    def _is_windows_test(node: ast.If) -> bool:
        """Report whether an ``if`` tests one of the two Windows spellings.

        Args:
            node: The ``if`` to examine.

        Returns:
            ``True`` when its test compares a known platform attribute against
            that attribute's Windows value.
        """
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
            return False
        left, right = test.left, test.comparators[0]
        if not (
            isinstance(left, ast.Attribute) and isinstance(left.value, ast.Name)
        ):
            return False
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return False
        return (left.value.id, left.attr, right.value) in WINDOWS_BRANCH_SPELLINGS

    branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and _is_windows_test(node)
    ]
    assert len(branches) == 1, (
        f"{function_name} has {len(branches)} Windows-only branches; exactly "
        "one is expected. Callers of this helper walk the node they are given, "
        "so a second one silently changes which code they assert about."
    )
    branch = branches[0]
    # `ast.walk` below descends into `orelse` too, so an `else` would fold the
    # non-Windows body into every assertion made about the Windows one. Keeping
    # the branch a single early return is what makes those assertions mean what
    # they say.
    assert not branch.orelse, (
        f"{function_name}'s Windows branch grew an `else`. Callers walk this "
        "node, so the non-Windows body would be searched as though it were the "
        "Windows one -- restore the early return, or teach every caller."
    )
    return branch


def test_terminate_process_tree_guards_only_on_the_return_code() -> None:
    """Pin the fallback guard to exactly `proc.returncode is None`

    **What this guard gates changed; the guard did not.** It used to stand
    between `terminate()` and `taskkill`, which is what made the tree-walking
    call unreachable in the ordinary case. `taskkill` now runs first and this
    guards the `terminate()` **fallback** instead — so the question it asks is
    the one that was always worth asking, "is our own worker still alive", and
    it is now asked at the point where the answer decides something. Every
    assertion below is unchanged, and deliberately so: retiring the case would
    have discarded three measured mutants and left `_windows_branch_of` with no
    caller.

    The guard used to read `proc.returncode is None and proc.pid is not None`.
    The second conjunct was dead — `WorkerProcess.pid` is declared `int`, and
    measured on CPython, `asyncio.subprocess.Process` assigns `self.pid =
    transport.get_pid()` once in `__init__` and never clears it: a probe printed
    the same integer before and after `wait()`.

    Asserting its *absence* would have been the weak test. This branch runs only
    on Windows, so nothing in this suite executes it, and mypy says nothing about
    a removed conjunct — which leaves three mutants a grep cannot tell apart:

    * deleting the whole `if` — `taskkill` then fires unconditionally;
    * inverting it to `is not None` — `taskkill` fires only at processes that
      have already exited, and never at the hung one it exists to reap;
    * dropping the wrong conjunct, leaving `proc.pid is not None`.

    So the test is positive and names the operator and both operands. All three
    mutants fail it.
    """
    branch = _windows_branch_of("_terminate_process_tree")

    guards = [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Attribute)
            and inner.attr == "returncode"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "proc"
            for inner in ast.walk(node.test)
        )
    ]
    assert len(guards) == 1, (
        f"Expected exactly one guard on `proc.returncode` inside "
        f"_terminate_process_tree's Windows branch, found {len(guards)}. The "
        "branch's other conditions test `killer`, not `proc`."
    )

    test = guards[0].test
    assert isinstance(test, ast.Compare), (
        "The tree-kill guard is no longer a single comparison. A `BoolOp` here "
        "means a conjunct came back -- the `proc.pid is not None` half was "
        "removed because WorkerProcess.pid is declared `int` and can never be "
        "None."
    )
    assert len(test.ops) == 1 and isinstance(test.ops[0], ast.Is), (
        "The tree-kill guard no longer tests `is None`. Inverted to `is not "
        "None` it fires taskkill only at processes that have already exited, "
        "which disables the tree-kill entirely and passes every other check in "
        "this suite."
    )
    assert (
        isinstance(test.left, ast.Attribute)
        and test.left.attr == "returncode"
        and isinstance(test.left.value, ast.Name)
        and test.left.value.id == "proc"
    ), "The tree-kill guard no longer tests `proc.returncode` on the left."
    assert len(test.comparators) == 1 and isinstance(
        test.comparators[0], ast.Constant
    ), "The tree-kill guard no longer compares against a constant."
    assert test.comparators[0].value is None, (
        "The tree-kill guard no longer compares `proc.returncode` against None."
    )


def _taskkill_call_in_the_windows_branch() -> ast.Call:
    """Locate the ``taskkill`` spawn inside the Windows branch.

    Returns:
        The ``create_subprocess_exec`` call whose first argument is the literal
        ``"taskkill"``.
    """
    branch = _windows_branch_of("_terminate_process_tree")
    call = next(
        (
            node
            for node in ast.walk(branch)
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "taskkill"
        ),
        None,
    )
    assert call is not None, (
        "_terminate_process_tree's Windows branch no longer spawns `taskkill`. "
        "It is the only thing on the platform that walks a process tree -- "
        "`terminate()` and `kill()` are both `TerminateProcess`, which reaches "
        "the named process only."
    )
    return call


def test_taskkill_is_the_first_kill_the_windows_branch_attempts() -> None:
    """Pin `taskkill` ahead of every other kill, which is the whole Windows fix

    The shipped order was `terminate()` -> wait 1.5s -> `if returncode is None`
    -> `taskkill`. On Windows CPython implements `terminate` as
    `TerminateProcess` and aliases `kill` to it: immediate, unconditional, and
    it kills the named process only. So the child was always dead well inside
    the wait, the guard was always false, and the one call that walks the tree
    essentially never ran. Descendants survived on Windows for exactly that
    reason -- not because the branch lacked a tree kill, but because it was
    placed where it could not be reached.

    Ordering is asserted by source position rather than by behaviour because no
    Linux run executes this branch at all. That is a real limit of this case and
    the reason the subsystem cases above are also required to run on Windows:
    this proves the code is arranged correctly, and only a Windows run proves it
    works.
    """
    branch = _windows_branch_of("_terminate_process_tree")
    taskkill_line = _taskkill_call_in_the_windows_branch().lineno

    # `proc.terminate()` / `proc.kill()`: any of these before the taskkill call
    # restores the defect, because whichever runs first is the one that kills
    # the process taskkill would have walked from.
    premature_kills = [
        node.lineno
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"terminate", "kill"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "proc"
        and node.lineno < taskkill_line
    ]
    assert not premature_kills, (
        f"a `proc.terminate()`/`proc.kill()` runs at line(s) {premature_kills}, "
        f"before `taskkill` at line {taskkill_line}. Either one kills the "
        "worker first, and taskkill then has no tree left to walk -- which is "
        "the shipped defect, restored."
    )

    premature_guards = [
        node.lineno
        for node in ast.walk(branch)
        if isinstance(node, ast.Attribute)
        and node.attr == "returncode"
        and isinstance(node.value, ast.Name)
        and node.value.id == "proc"
        and node.lineno < taskkill_line
    ]
    assert not premature_guards, (
        f"`proc.returncode` is tested at line(s) {premature_guards}, before "
        f"`taskkill` at line {taskkill_line}. A live process is exactly the "
        "case taskkill exists for, so a guard in front of it can only ever "
        "suppress the call that was needed."
    )


def test_the_taskkill_argv_asks_for_the_tree_and_names_the_worker() -> None:
    """Pin `/T`, which is the only reason this call reaches a descendant

    Ordering alone is not enough. `taskkill /F /PID <pid>` runs in exactly the
    same place, passes the ordering case unchanged, and kills one process --
    leaving the browser behind and the defect intact. `/T` is what makes it a
    tree kill, so it is named here explicitly.

    `str(proc.pid)` is asserted as a call rather than by value because the pid
    is not knowable from the source; what matters is that the argv is built from
    *this* process's pid and not from a captured or hard-coded one.
    """
    call = _taskkill_call_in_the_windows_branch()

    literals = [
        argument.value
        for argument in call.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    assert literals == ["taskkill", "/T", "/F", "/PID"], (
        f"the taskkill argv literals are {literals}. `/T` is what walks the "
        "tree; without it this is a single-process kill wearing the name of a "
        "tree kill."
    )

    names_the_worker = any(
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id == "str"
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "pid"
            for inner in ast.walk(argument)
        )
        for argument in call.args
    )
    assert names_the_worker, (
        "the taskkill argv does not carry `str(proc.pid)`, so whatever tree it "
        "walks is not this worker's."
    )


@pytest.mark.subsystem
def test_the_parent_map_reads_this_process_s_real_parent() -> None:
    """Prove the `/proc` reader agrees with the kernel, or is honestly empty

    The pure walk above is driven with a hand-built mapping, so nothing there
    would notice if the reader that produces the real one were wrong -- a
    mis-parsed field, a missed directory, an off-by-one in the stat line. This
    is the case that ties the two together, and it needs no fixture: this
    process has a parent and `os.getppid()` names it.

    Both polarities, because "returns nothing" is a supported answer and not a
    failure. Off Linux there is no `/proc`, the map is empty by design, and the
    terminator degrades to killing the worker alone. Asserting only the Linux
    half would leave a reader that silently returned nothing *on Linux* looking
    exactly like correct behaviour elsewhere.
    """
    parent_map = worker_runner._read_parent_map()

    if not Path("/proc/self/stat").exists():
        assert parent_map == {}, (
            "there is no /proc here, so the map must be empty rather than "
            "partially populated from somewhere else."
        )
        return

    assert parent_map.get(os.getpid()) == os.getppid(), (
        f"the map says this process's parent is "
        f"{parent_map.get(os.getpid())}; the kernel says {os.getppid()}."
    )
    # Not merely non-empty: a reader that returned one row would satisfy the
    # assertion above and find no descendants for any worker.
    assert len(parent_map) > 1


def test_every_moved_symbol_is_documented() -> None:
    """Hold the module to the project's every-symbol docstring rule

    Thirteen of these fourteen arrived undocumented. They moved verbatim out of
    `universal_html.py`, where they had none either, because an extraction that
    also rewrites what it moves cannot be reviewed as an extraction — so the gap
    was carried, deliberately, into the step that annotates the same signatures.

    Scoped to the moved surface rather than to `src/`: the rest of the tree has
    the same gap, closing it is no current step's business, and a repo-wide guard
    would be red on arrival and get muted.

    Read off `ast`, not `__doc__`. Two of these are dataclasses, and
    `@dataclass` synthesises a `__doc__` from the field list when none is
    written, so an attribute check reports both as documented when neither is.
    """
    source = Path(worker_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=worker_runner.__file__)
    defined = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    missing = [name for name in PROCESS_MANAGEMENT_SURFACE if name not in defined]
    assert not missing, (
        f"{missing} are named in PROCESS_MANAGEMENT_SURFACE but not defined at "
        "module scope in worker_runner.py."
    )

    undocumented = [
        name
        for name in PROCESS_MANAGEMENT_SURFACE
        if not (ast.get_docstring(defined[name]) or "").strip()
    ]
    assert not undocumented, (
        f"These symbols in worker_runner.py carry no docstring: {undocumented}. "
        "The project's rule is every class, method and function."
    )
    assert ast.get_docstring(tree), "worker_runner.py has lost its module docstring."

    # Both this case and the ownership case above iterate the tuple, so without
    # this a fifteenth module-level symbol -- undocumented -- is invisible to
    # both, and the rule silently stops reaching the module it is scoped to.
    extra = sorted(set(defined) - set(PROCESS_MANAGEMENT_SURFACE))
    assert not extra, (
        f"worker_runner.py defines {extra} outside PROCESS_MANAGEMENT_SURFACE, "
        "so neither the ownership case nor the docstring rule reaches them. Add "
        "them to the tuple."
    )
