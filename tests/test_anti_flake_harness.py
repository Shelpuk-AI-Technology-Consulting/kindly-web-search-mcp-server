"""Calibration for the anti-flake test harness.

``tests/harness/anti_flake.py`` implements the obligations section 5.4 of
``.system_design/TEST_SUITE.md`` places on every test that starts something
real: a readiness handshake rather than a sleep, ephemeral ports, a per-child
deadline, cleanup keyed on the pids the test spawned, and the child's own output
attached to any failure.

An instrument needs its own calibration, and these cases are it. Most drive one
helper against a real process or a real socket and assert what it produced.

**The fixed point is the fixture child script, not this harness.**
``tests/test_worker_child_fixture.py`` deliberately does *not* run through these
helpers: that module calibrates the fixture child, and driving it through a
second instrument would leave every failure unable to say which of the two was
broken. The dependency runs one way only -- these cases use the fixture child,
and its own cases use nothing from here but the two platform probes, which are
the pieces this module pins hardest.

**Four cases are hermetic, and that is not a preference.** The walk, its stat
parse, and the kill set it produces are driven against injected values with no
process in existence, because the obvious mutation of a tree reaper -- one that
loses its exclusions -- signals the caller's own process group when it is run
against a real tree, which kills the pytest session and its shell and produces
no report at all. That is the same argument production's own signaller makes for
injecting its signal primitives.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from _pytest.outcomes import Failed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.harness.anti_flake import (
    REAP_TIMEOUT_SECONDS,
    build_kill_order,
    collect_descendants,
    descendants_of,
    kill_pid,
    parse_parent_pid,
    reserve_ephemeral_port,
    spawned_child,
    wait_for_accepting_port,
    wait_until_gone,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The fixture child, which is the only artefact in this tree with a process
#: tree to reap. Imported from its own calibration module rather than
#: re-derived, so there is one path constant; the dependency runs this way only,
#: and that module takes nothing from the harness but the two platform probes.
FIXTURE_CHILD = REPO_ROOT / "tests" / "child_processes" / "worker_child.py"

#: Readiness budget for the cases that spawn a child. Well under the
#: thirty-second default and still two orders of magnitude above the forty
#: milliseconds a Python interpreter here actually needs, so it can only fire on
#: a child that is genuinely not going to speak. It is a budget, never an
#: assertion: no case checks how long readiness took.
SHORT_READINESS_SECONDS = 5.0

#: Per-child deadline for the cases that assert it fires. Small, and it can be:
#: the deadline is measured from the handshake rather than from the spawn, so a
#: slow interpreter start spends the readiness budget above and none of this.
SHORT_DEADLINE_SECONDS = 0.5

#: Ceiling on waiting for a signalled process to disappear. Generous, because it
#: bounds a *negative* -- it can only fail if something is still alive -- so a
#: loaded runner costs nothing and a leak is still caught.
DEATH_TIMEOUT_SECONDS = 30.0

#: Bound on the port-wait cases. Long enough that the elapsed-time assertion is
#: not measuring scheduler noise, short enough to spend twice per run.
PORT_WAIT_SECONDS = 1.0

#: Deadline for the two cases that must let one expire while they watch, and
#: then wait it out again. Small for the same reason, and separate from
#: :data:`SHORT_DEADLINE_SECONDS` so that lowering one to speed a case up cannot
#: silently change what the other is measuring.
BRIEF_DEADLINE_SECONDS = 0.5

#: An artefact that logs before it announces itself, which is the ordinary shape
#: and the one the fixture child does not have. Not the fixture child precisely
#: because of that: its readiness frame is its first output, so it cannot tell a
#: harness that matches the marker from one that returns on line one.
CHATTY_SOURCE = (
    "import sys, time\n"
    "sys.stderr.buffer.write(b'starting up, not ready yet\\n')\n"
    "sys.stderr.buffer.write(b'ready-frame harness.ready\\n')\n"
    "sys.stderr.buffer.flush()\n"
    "time.sleep(60)\n"
)

def _recording_killer(seen: list[int]) -> Any:
    """Build a killer that records the pid **and then kills it**.

    Recording alone is not enough, and the difference is a leaked process rather
    than a slow case. A killer that only appends leaves the context manager's own
    teardown unable to reap a hanging child: the reap waits out its full bound,
    the pipe pumps wait out theirs, and the child then survives to its own
    five-minute backstop. Measured at thirty-three seconds and one leaked
    interpreter per run before this helper existed.

    Args:
        seen: List every signalled pid is appended to, in order.

    Returns:
        A callable suitable for the harness's ``kill`` parameter.
    """
    def record_and_kill(pid: int) -> None:
        seen.append(pid)
        kill_pid(pid)

    return record_and_kill


#: The control process's program, byte-identical to what the fixture child's
#: descendants run, so a reaper matching on a command line cannot tell them
#: apart. It outlives nothing: the case kills it in its own ``finally``.
CONTROL_SOURCE = "import time\nwhile True:\n    time.sleep(0.25)\n"

#: The harness package, addressed by path for the import guard, which reads
#: syntax trees rather than the imported modules' namespaces. The whole package
#: and not one module in it: a helper added beside ``anti_flake.py`` inherits
#: every reason the guard exists.
HARNESS_PACKAGE = REPO_ROOT / "tests" / "harness"

#: Ceiling on the cyclic-map walk. Generous against a walk that terminates --
#: the map has five entries -- and finite against one that does not, which is
#: the only distinction this number has to make.
CYCLE_WALK_TIMEOUT_SECONDS = 5.0

#: Third-party packages the harness may import. Empty, and deliberately a set
#: rather than an absent check: nothing needs one today, and adding one should
#: be an edit here rather than something that arrives unnoticed.
ALLOWED_THIRD_PARTY: frozenset[str] = frozenset()


def test_the_harness_never_imports_the_code_it_will_be_used_to_measure() -> None:
    """Keep the instrument independent of the subject

    The claim that matters, and it is one claim rather than the two it looks
    like. This harness will be used to reap the process trees that the parent-side
    process runner's own lifecycle cases build -- and that runner ships its own
    ``/proc`` walk, which a later step exists to test. A harness that imported it
    would reap with the code under test: a defect in that walk would leave a
    leaked browser *and* a green teardown, and the case written to catch it would
    be measuring the defect with itself.

    So the parent-pid parse and the descendant walk here are deliberate
    duplicates of production's, and this is what keeps them duplicates.

    The stdlib inventory is asserted **separately and for a weaker reason.**
    Unlike the fixture child -- where importing anything outside the standard
    library would stop the script starting at all, because it is spawned by path
    into an environment nobody controls -- this package is imported inside the
    pytest process, where `pytest` itself would be a perfectly reasonable
    dependency if a helper ever needed to skip. Nothing needs one today, so the
    inventory is empty; the assertion exists to make adding one a decision
    rather than a drift.

    Asserted over syntax trees rather than by importing, so the check holds for a
    branch that never executes.
    """
    package = "kindly_web_search_mcp_server"
    roots: set[str] = set()
    modules = sorted(HARNESS_PACKAGE.rglob("*.py"))
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # A relative import has no module name to attribute, and would
                # be a bug in its own right here.
                assert node.level == 0, f"{module.name} imports relatively"
                if node.module:
                    roots.add(node.module.split(".")[0])

    assert modules, "the sweep read no modules, so it is asserting nothing"
    assert roots, "the sweep found no imports, so it is asserting nothing"
    assert package not in roots, (
        f"the harness imports {package}, which is the code it exists to measure"
    )
    outside = sorted(roots - set(sys.stdlib_module_names) - ALLOWED_THIRD_PARTY)
    assert not outside, f"the harness gained third-party imports: {outside}"


def test_the_parent_pid_parse_survives_a_process_name_containing_a_bracket() -> None:
    """Read the parent from a stat line the kernel does not escape

    The second field of ``/proc/<pid>/stat`` is the executable name in brackets,
    and the kernel neither escapes nor rejects a space or a closing bracket
    inside it. A whitespace split therefore reads some other field entirely --
    and *silently*, yielding a plausible small integer that this harness would
    then go on to kill. Everything after the **last** bracket is read for that
    reason.

    Driven with a name carrying both traps at once, because a parser that
    handles one and not the other is the likely half-fix. The expected parent is
    a value no other field of the line holds, so a parser reading the wrong
    field cannot pass by coincidence.

    Duplicated from production's own parse rather than imported. The reason is
    recorded in section 5.4: production's walk is the subject of a later step,
    and a harness that reaped with it could not be used to measure it.
    """
    stat_text = "4242 (bad ) name) S 1234 4242 4242 0 -1 4194304 0 0 0\n"

    assert parse_parent_pid(stat_text) == 1234


@pytest.mark.parametrize(
    ("stat_text", "reason"),
    [
        ("", "an empty read, which is what an exiting process leaves"),
        ("4242 (python3", "a line truncated inside the name"),
        ("4242 (python3) S", "a line with no parent field yet"),
        ("4242 (python3) S notanumber 4242", "a parent field that is not a number"),
    ],
)
def test_an_unreadable_stat_line_yields_no_parent(stat_text: str, reason: str) -> None:
    """Answer "no parent" rather than guessing, on every shape a truncated read takes

    Processes exit underneath this scan by definition, so a short or empty read
    is ordinary here rather than exceptional. Each shape is driven separately
    because they leave the parse at four different points, and one ``None``
    return covering all four would hide a parser that raises on the others.

    Args:
        stat_text: The line to parse.
        reason: What that line stands for, quoted in the failure.
    """
    assert parse_parent_pid(stat_text) is None, reason


def test_the_walk_returns_every_generation_and_never_the_root() -> None:
    """Reach a descendant nobody announced, at any depth

    The claim the whole harness rests on. Production's tree is worker -> browser
    -> renderers and only the first of those announces anything, so a reaper
    that can only kill what it was told about leaves the browser running. Three
    generations rather than two, because a walk that recursed once would pass a
    two-generation case.

    The root is excluded because the caller kills it separately and last: a walk
    that returned it would have the caller kill it twice, and the second kill
    would land on whatever recycled its pid.

    A sibling branch is present so the case can also fail a walk that follows
    parentage upward instead of downward, or one that returns the whole map.
    """
    parent_map = {
        100: 1,  # the root's own parent, which must not be reached
        200: 100,  # the root
        300: 200,
        400: 300,
        500: 400,
        600: 100,  # a sibling of the root, never a descendant of it
    }

    assert sorted(collect_descendants(parent_map, 200)) == [300, 400, 500]


def test_the_walk_terminates_on_a_map_that_contains_a_cycle() -> None:
    """Stop, on a snapshot that cannot happen and does

    The parent map is assembled from many files that were not read atomically
    while pids were being recycled, so it can contain a cycle or a process that
    is its own parent. Neither is a real process tree; both are real *readings*
    of one. A walk that loops here does not fail -- it hangs inside a ``finally``
    during teardown, which is the least diagnosable failure this module could
    ship.

    **Run on a thread with a bounded join, and that is the whole point of the
    case.** Called directly, the mutation this exists to catch -- deleting the
    visited set -- does not turn this module red, it makes pytest never return,
    which is a worse outcome than the defect. Measured: the direct form was
    killed by an external 25-second timeout with no report at all. The daemon
    thread is what converts a hang into a sentence; it is deliberately not
    joined afterwards, because a thread spinning inside a mutant will not stop.

    Asserted as a returned value rather than only as termination, so a walk that
    escaped the loop by dropping a generation would still fail.
    """
    parent_map = {
        200: 1,
        300: 200,
        400: 300,
        500: 400,
        300_000: 300_000,  # its own parent
    }
    parent_map[200] = 500  # closes the cycle: 200 -> 500 -> 400 -> 300 -> 200

    walked: list[list[int]] = []
    worker = threading.Thread(
        target=lambda: walked.append(collect_descendants(parent_map, 200)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=CYCLE_WALK_TIMEOUT_SECONDS)

    assert not worker.is_alive(), (
        f"the walk was still running after {CYCLE_WALK_TIMEOUT_SECONDS}s on a "
        f"parent map containing a cycle, so it has no visited set"
    )
    assert sorted(walked[0]) == [300, 400, 500]


def test_the_kill_order_excludes_this_process_and_init() -> None:
    """Refuse to signal the caller, whatever the snapshot said

    This is the exclusion that is *live* rather than defensive. The map admits
    cycles and recycled pids, so the caller's own pid can genuinely appear in a
    descendant list; and pid 1 appears the moment a walked process exits and its
    children reparent between the read and the kill. Signalling either from a
    test's teardown ends the pytest session, so this case is written against
    injected values and spawns nothing.

    The root is asserted last, and the descendants before it. A reaper that
    killed the root first would reparent everything below it to pid 1, where the
    pids it holds name processes it no longer has any claim to.
    """
    own_pid = os.getpid()

    order = build_kill_order([300, own_pid, 1, 400], root=200, own_pid=own_pid)

    assert order == [300, 400, 200]


def test_the_kill_order_refuses_a_root_that_is_this_process() -> None:
    """Survive being handed the caller's own pid as the tree to reap

    A caller that passes ``os.getpid()`` has made a mistake, and the mistake's
    cost is the whole test session. The exclusion is applied to the root as well
    as to the descendants for that reason: there is no reading of "reap this
    tree" under which killing the process asking is the right answer.
    """
    own_pid = os.getpid()

    assert build_kill_order([300], root=own_pid, own_pid=own_pid) == [300]


def test_the_harness_never_names_a_group_signalling_primitive() -> None:
    """Keep a process-group kill out of a harness whose group holds the test runner

    Production signals descendant process *groups* before it signals pids,
    because a browser leads a group of its own and one call takes it together
    with every renderer. That optimisation is correct there and forbidden here:
    a test process shares its group with everything it spawns, so the same call
    written in a test reaches the pytest session, the terminal, and whatever
    started them. Measured on production's topology -- the worker's group is the
    server's group.

    Asserted over the harness's syntax trees rather than by calling anything,
    because the only run-time observation of this mutation is the absence of a
    test report. The sweep asserts it read something, so an empty package cannot
    pass it, and it names attributes rather than imports so
    ``os.killpg(...)`` is caught however ``os`` arrived.
    """
    forbidden = {"killpg", "setpgid", "getpgid"}
    seen: list[str] = []
    for module in sorted(HARNESS_PACKAGE.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                seen.append(node.attr)
            elif isinstance(node, ast.Name):
                seen.append(node.id)

    assert seen, "the sweep read no names, so it is asserting nothing"
    named = sorted(set(seen) & forbidden)
    assert not named, (
        f"the harness names {named}, and a process-group signal from a test "
        f"reaches the pytest session's own group"
    )


@pytest.mark.subsystem
def test_readiness_returns_on_the_marker_the_caller_asked_for() -> None:
    """Wait for a line the caller names, not for one this module knows about

    A harness that hard-coded the fixture child's frame prefix would know one
    artefact's protocol, and the two artefacts still to come -- a server over a
    real socket and a browser pool -- announce themselves differently. The
    marker is a parameter for that reason, and this case passes one that is a
    *substring* of the announcing line rather than the whole of it, because a
    caller knows the stage its artefact announces and not the envelope around
    it.

    Driven against a child that is **not** the fixture child, and that is not
    incidental. The fixture child announces on its very first line, so a harness
    that ignored the marker entirely and returned on line one would pass against
    it -- measured, that mutant survived. This artefact writes a line of noise
    first.

    No upper bound on the elapsed time is asserted. A millisecond threshold
    measures a loaded runner and an antivirus scanner's process-start delay, and
    section 5.2a forbids it; the duration is printed as telemetry instead.
    """
    with spawned_child(
        [sys.executable, "-c", CHATTY_SOURCE],
        readiness_marker=b"harness.ready",
        readiness_timeout=SHORT_READINESS_SECONDS,
    ) as child:
        assert b"harness.ready" in child.ready_line
        # The line before it is kept, not discarded. Two claims in one: a
        # harness that returned on the *first* line would have announced on the
        # noise -- measured, that mutation survived a case driving the fixture
        # child, whose readiness frame happens to be its first output -- and an
        # artefact that logs before it announces is the ordinary case, so what
        # it said is the first thing a reader wants when the marker never comes.
        assert child.preamble == [b"starting up, not ready yet\n"]


@pytest.mark.subsystem
def test_a_child_that_dies_talking_is_diagnosed_by_its_exit_code() -> None:
    """Blame the command line, not the machine, when a child exits mid-handshake

    The common failure here is not a slow runner, it is a flag the artefact does
    not have -- a case is written before the flag it drives, so it meets
    ``argparse`` first. That is the awkward shape rather than the easy one: a
    silent death is caught by polling the exit code, but ``argparse`` writes its
    usage to stderr and *then* exits 2, so a line does arrive and a naive wait
    keeps waiting for a marker that will never come.

    Without this the report would name a thirty-second timeout, which is a
    diagnosis three steps from the cause.
    """
    with (
        pytest.raises(AssertionError) as excinfo,
        spawned_child(
            [sys.executable, str(FIXTURE_CHILD), "--no-such-flag"],
            readiness_marker=b"fixture.ready",
        ),
    ):
        pass

    message = str(excinfo.value)
    assert "exited with code 2" in message
    assert "--no-such-flag" in message
    assert "usage:" in message


@pytest.mark.subsystem
def test_a_port_wait_returns_once_something_is_listening() -> None:
    """Hand-shake with a socket, for an artefact that has no pipe to speak on

    Section 5.4 allows a readiness handshake to wait for a known line **or** an
    accepting port. The second form is what a server started as a separate
    process needs, and it is the form the next two subsystem steps will use.
    """
    port = reserve_ephemeral_port()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        wait_for_accepting_port("127.0.0.1", port, timeout=PORT_WAIT_SECONDS)
    finally:
        listener.close()


@pytest.mark.subsystem
def test_a_port_wait_spends_its_whole_bound_when_nothing_accepts() -> None:
    """Fail at the bound rather than returning, and say which port

    The mutation this exists to kill is a wait that returns unconditionally, and
    the only observation that separates it from a correct one is that the
    correct one takes its bound.

    **Only the lower bound is asserted.** An upper one would be a startup budget
    by another name, measuring the runner rather than this code.

    The dead port is held **bound but not listening**, not merely chosen and
    left alone. A port nobody holds can be taken by anything between the choice
    and the poll, which would make this case a flake; a bound socket that never
    calls ``listen`` cannot be taken and can never accept.
    """
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]

        started = time.monotonic()
        with pytest.raises(AssertionError) as excinfo:
            wait_for_accepting_port("127.0.0.1", port, timeout=PORT_WAIT_SECONDS)
        elapsed = time.monotonic() - started
    finally:
        holder.close()

    assert elapsed >= PORT_WAIT_SECONDS
    assert str(port) in str(excinfo.value)
    assert "127.0.0.1" in str(excinfo.value)


def test_a_reserved_port_is_never_one_a_live_listener_holds() -> None:
    """Give out a port nothing is using, which is the whole point of the helper

    Two tests running at once must not choose the same port, and the only
    portable way to be sure of that is to let the kernel choose. The mutant this
    case is written against is a reservation hard-coded to **the port the
    listener holds** -- not to an arbitrary constant, which would pass this and
    be caught by the next case instead.

    Twenty draws rather than one: a single draw could miss a broken
    implementation that returns the held port only occasionally.
    """
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        held = listener.getsockname()[1]

        drawn = [reserve_ephemeral_port() for _ in range(20)]
    finally:
        listener.close()

    assert held not in drawn


def test_a_reserved_port_can_be_bound_immediately() -> None:
    """Leave the port free, rather than holding the socket that found it

    A reservation that kept its socket open would be useless: the caller's whole
    reason for asking is to put its own listener there. This is the claim an
    implementation can actually fail, and it is what makes the helper's
    check-to-use race the *only* thing standing between the two.
    """
    port = reserve_ephemeral_port()
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        assert listener.getsockname()[1] == port
    finally:
        listener.close()


@pytest.mark.subsystem
def test_a_hanging_child_is_killed_at_its_deadline_with_its_whole_tree(
    tmp_path: Path,
) -> None:
    """Kill a child that will not stop, and everything it started

    The step's own verify clause, in one case: a hanging fixture child is killed
    at the deadline and its PID tree is gone.

    **The harness is told the root pid and nothing else.** The chain is three
    generations deep and only the first announces itself, so a reaper that acted
    on the announcement would leave two processes running. That is production's
    shape -- worker, then a browser that announces nothing, then its renderers.

    The pid file is read as an **oracle only**: the harness never opens it, and
    the script's own claim about it is pinned separately, against the depth the
    command line asked for, in the fixture child's calibration module. Neither
    claim rests on the other.

    Args:
        tmp_path: Per-test directory for the oracle.
    """
    pid_file = tmp_path / "pids.json"
    with spawned_child(
        [
            sys.executable,
            str(FIXTURE_CHILD),
            "--pid-file",
            str(pid_file),
            "--spawn-grandchild",
            "--grandchild-depth",
            "3",
            "--grandchild-new-session",
            "--hang",
        ],
        readiness_marker=b"fixture.ready",
        readiness_timeout=SHORT_READINESS_SECONDS,
        deadline=SHORT_DEADLINE_SECONDS,
    ) as child:
        chain = [entry["pid"] for entry in json.loads(pid_file.read_text())["chain"]]
        assert len(chain) == 3

        # The walk, before anything is killed. Asserted against the oracle
        # rather than against a count, so a walk that found three *other*
        # processes would fail too.
        if sys.platform.startswith("linux"):
            assert sorted(descendants_of(child.proc.pid)) == sorted(chain)

        assert wait_until_gone(child.proc.pid, timeout=DEATH_TIMEOUT_SECONDS)
        assert child.deadline_fired

    if sys.platform.startswith("linux") or sys.platform == "win32":
        for pid in chain:
            assert wait_until_gone(pid, timeout=DEATH_TIMEOUT_SECONDS), (
                f"generation pid {pid} outlived the tree kill"
            )
    else:
        # The honest half of the degradation, asserted rather than assumed:
        # without `/proc` there is no enumeration, and the reap still reaches
        # the root without raising. A platform this suite has no lane for gets a
        # stated behaviour instead of an untested one.
        assert descendants_of(child.proc.pid) == []


@pytest.mark.subsystem
def test_a_process_with_an_identical_command_line_survives_the_reap(
    tmp_path: Path,
) -> None:
    """Kill by kinship, never by name

    Section 5.4 forbids a name scan in as many words, and the reason is that a
    developer running this suite has their own browser open. A scan for
    ``chrome`` reaches it; so does a scan for ``python``, which is what every
    process in this tree is called.

    The control is spawned by **this test**, with a command line byte-identical
    to the descendants', and is not a descendant of the child. Any reaper that
    matched on a name, an executable, or a command line kills it. Measured the
    same way in production, where the control stood in for a pooled browser.
    """
    pid_file = tmp_path / "pids.json"
    control = subprocess.Popen(
        [sys.executable, "-c", CONTROL_SOURCE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with spawned_child(
            [
                sys.executable,
                str(FIXTURE_CHILD),
                "--pid-file",
                str(pid_file),
                "--spawn-grandchild",
                "--grandchild-depth",
                "2",
                "--hang",
            ],
            readiness_marker=b"fixture.ready",
            readiness_timeout=SHORT_READINESS_SECONDS,
            deadline=SHORT_DEADLINE_SECONDS,
        ) as child:
            chain = [
                entry["pid"] for entry in json.loads(pid_file.read_text())["chain"]
            ]
            assert wait_until_gone(child.proc.pid, timeout=DEATH_TIMEOUT_SECONDS)

        if sys.platform.startswith("linux") or sys.platform == "win32":
            for pid in chain:
                assert wait_until_gone(pid, timeout=DEATH_TIMEOUT_SECONDS)

        # Observed after the tree is gone, so the claim is "it survived a reap
        # that completed", not "it had not been reached yet".
        assert control.poll() is None, (
            "a process with the same command line as the descendants was killed "
            "by a reap it had no kinship with"
        )
    finally:
        control.kill()
        control.wait(timeout=REAP_TIMEOUT_SECONDS)


@pytest.mark.subsystem
def test_a_child_that_exits_inside_its_deadline_is_never_signalled() -> None:
    """Leave a well-behaved child alone, including after its deadline passes

    The pid-reuse hazard the deadline brings with it. Once a child has been
    reaped its pid is free -- immediately on Windows, quickly on Linux under a
    container's low ``pid_max`` -- so a watchdog that fires afterwards signals a
    stranger. That is the harm the name-scan rule exists to prevent, reached by
    a different route.

    **The wait past the deadline happens inside the block**, which is what makes
    the guard reachable. A watchdog whose deadline has not expired calls nothing
    whether or not it is guarded, and one whose block has ended has been
    disarmed -- so the obvious shape of this case, exit and assert, cannot tell
    a guarded watchdog from an unguarded one. Measured: with the wait outside
    the block, deleting the guard left this case passing.

    What closes the hazard is the ``poll()`` the watchdog takes first, which is
    the guard CPython puts inside ``Popen.send_signal`` for the same reason. A
    reaped child answers it with a code; an unreaped child cannot have had its
    pid recycled, because the ``Popen`` still holds it.

    Observed through an injected killer because there is nothing else to
    observe: a correct implementation and a broken one differ only in whether a
    signal was sent to a pid that may by then belong to somebody else.
    """
    signalled: list[int] = []
    with spawned_child(
        [sys.executable, str(FIXTURE_CHILD), "--exit-code", "0"],
        readiness_marker=b"fixture.ready",
        readiness_timeout=SHORT_READINESS_SECONDS,
        deadline=BRIEF_DEADLINE_SECONDS,
        kill=_recording_killer(signalled),
    ) as child:
        assert child.wait_for_exit() == 0
        # Inside the block, past the deadline. The watchdog therefore *fires*
        # here -- it has not been disarmed, because the block has not ended --
        # against a child that has already been reaped. That is the only shape
        # in which the guard is reached at all: with the wait outside the block
        # the disarm handles it first and the mutation that deletes the guard
        # survives. Measured.
        time.sleep(BRIEF_DEADLINE_SECONDS)

    assert not signalled, (
        "the deadline watchdog signalled a pid after its child had been reaped"
    )
    assert not child.deadline_fired


@pytest.mark.subsystem
def test_the_deadline_watchdog_does_not_outlive_the_block_it_bounds() -> None:
    """Take the watchdog thread down with the block, rather than leaving it armed

    A separate claim from the one above, and separated because they fail for
    different reasons. That one says a *fired* watchdog signals nothing once its
    child has been reaped; this one says the watchdog does not fire at all once
    the block is over. Without it every case that sets a deadline leaves a timer
    thread behind for the rest of its interval -- one per case, all of them
    holding a closure over a dead process.

    The deadline is allowed to pass **after** the block, with the killer still
    watching, which is the only interval in which an armed watchdog and a
    disarmed one differ.
    """
    signalled: list[int] = []
    with spawned_child(
        [sys.executable, str(FIXTURE_CHILD), "--hang"],
        readiness_marker=b"fixture.ready",
        readiness_timeout=SHORT_READINESS_SECONDS,
        deadline=BRIEF_DEADLINE_SECONDS,
        kill=_recording_killer(signalled),
    ) as child:
        pass

    # The child was still hanging when the block ended, so teardown reaped it --
    # that is one signal, and it is not the watchdog's.
    assert signalled == [child.proc.pid] or child.proc.pid in signalled
    reaped_at_teardown = list(signalled)

    time.sleep(BRIEF_DEADLINE_SECONDS * 2)

    assert signalled == reaped_at_teardown, (
        "the deadline watchdog was still armed after its block had ended"
    )
    assert not child.deadline_fired


@pytest.mark.subsystem
def test_a_payload_past_the_pipe_capacity_neither_blocks_nor_is_truncated() -> None:
    """Drain both pipes, so a child writing a page cannot wedge itself

    A Linux pipe buffers 64 KiB and Windows promises only "the default buffer
    size". A harness reading only stderr therefore blocks the child inside its
    stdout write as soon as the payload passes that, and every later assertion
    about a child that is "still running" then passes for the wrong reason --
    because a child blocked in ``write`` *is* still running, which is why that
    observation cannot be the assertion here.

    **The falsifying observable is that the child exits.** A stderr-only harness
    deadlocks and this case fails at its bound; the byte-for-byte comparison
    then catches a drain that finished early.
    """
    size = 200_000
    with spawned_child(
        [sys.executable, str(FIXTURE_CHILD), "--stdout-bytes", str(size)],
        readiness_marker=b"fixture.ready",
        readiness_timeout=SHORT_READINESS_SECONDS,
    ) as child:
        assert child.wait_for_exit(timeout=DEATH_TIMEOUT_SECONDS) == 0
        payload = child.stdout_bytes()

    assert len(payload) == size


@pytest.mark.subsystem
@pytest.mark.parametrize(
    ("raiser", "expected_type"),
    [
        (lambda: (_ for _ in ()).throw(AssertionError("the original complaint")), AssertionError),
        (lambda: pytest.fail("the original complaint"), Failed),
    ],
    ids=["assertion-error", "pytest-fail"],
)
def test_a_failure_inside_the_block_carries_the_childs_own_output(
    raiser: Any, expected_type: type[BaseException]
) -> None:
    """Attach what the child said to a failure about what the child did

    The child is a separate process, so pytest shows nothing of it. Without this
    every failure reads "the child did not do what was expected" with no way to
    see what it did instead.

    **Both failure shapes are driven, and the second is the one that matters.**
    ``pytest.fail`` raises ``Failed``, which derives from ``BaseException`` and
    **not** from ``AssertionError`` -- so a handler written against
    ``AssertionError`` alone, which is what the fixture child's own local helper
    uses, silently drops the capture for the majority of real subsystem
    failures.

    The capture is added as an exception **note** rather than by re-raising a
    new exception. An arbitrary exception cannot be rebuilt with a longer
    message -- ``subprocess.TimeoutExpired`` takes no such argument -- so
    "attach the capture and keep the class" is not implementable by wrapping.
    The consequence for this case: notes are not part of ``str(exception)``, so
    the assertion reads them from ``__notes__``.

    Args:
        raiser: Raises the failure under test.
        expected_type: The class that must survive the attachment.
    """
    with (
        pytest.raises(expected_type) as excinfo,
        spawned_child(
            [sys.executable, str(FIXTURE_CHILD), "--emit-frame", "alpha", "--hang"],
            readiness_marker=b"fixture.ready",
            readiness_timeout=SHORT_READINESS_SECONDS,
            deadline=SHORT_DEADLINE_SECONDS,
        ),
    ):
        raiser()

    assert type(excinfo.value) is expected_type
    assert "the original complaint" in str(excinfo.value)
    notes = "".join(getattr(excinfo.value, "__notes__", []))
    assert "argv:" in notes
    # The child's real frame, not a placeholder: this is what distinguishes an
    # attachment that captured the stream from one that captured an empty one.
    assert "alpha" in notes


@pytest.mark.subsystem
def test_a_failure_between_spawn_and_setup_still_reaps_the_child() -> None:
    """Open the cleanup the moment the process exists, not once it is wired up

    Everything between ``Popen`` returning and the pipe pumps running can still
    fail -- ``Thread.start`` raises under thread exhaustion -- and a failure
    there leaves a running child with nothing to reap it but its own
    five-minute backstop.

    The thread factory is a parameter for this case alone, and that is the
    point: the alternative was a docstring claiming the ``try`` opens early
    enough, which is the shape this repository has already shipped untested
    once. A requirement belongs in a case or in the reviewer's guard list, and
    there is no third place.
    """
    spawned: list[int] = []

    def refuse(*_args: Any, **_kwargs: Any) -> threading.Thread:
        raise RuntimeError("no threads available")

    with (
        pytest.raises(RuntimeError, match="no threads available"),
        spawned_child(
            [sys.executable, str(FIXTURE_CHILD), "--hang"],
            readiness_marker=b"fixture.ready",
            readiness_timeout=SHORT_READINESS_SECONDS,
            thread_factory=refuse,
            on_spawn=spawned.append,
        ),
    ):
        pass

    assert spawned, "the child was never spawned, so this case proves nothing"
    assert wait_until_gone(spawned[0], timeout=DEATH_TIMEOUT_SECONDS), (
        "a failure before the pipe pumps started left the child running"
    )
