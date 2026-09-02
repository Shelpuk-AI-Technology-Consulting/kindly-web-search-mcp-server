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
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

    `_run_worker_command` resolves ``KINDLY_HTML_TOTAL_TIMEOUT_SECONDS`` from the
    **parent's** environment, not from the environment it hands the child, so an
    ambient value silently replaces the budget each case passes. A shell
    exporting a small one would turn every case here red for a reason no
    assertion names; a large one would make the clamping assertions vacuous.

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


def test_worker_runner_owns_the_process_management_surface() -> None:
    """Name what moved, so a partial move back is a failure rather than a drift

    A list, not a spot check. The extraction is only worth its diff if the whole
    block travelled: leaving one stream reader or the terminator behind puts
    unhermetic code back inside the gated module without any single assertion
    above noticing.
    """
    expected = (
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
        "_subprocess_launch_options",
        "_StdoutAccumulator",
        "_StderrAccumulator",
    )

    missing = [name for name in expected if not hasattr(worker_runner, name)]
    assert not missing, f"worker_runner.py is missing {missing}"

    strays = [
        name
        for name in expected
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


def test_asyncio_wait_is_not_reachable_through_universal_html() -> None:
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
