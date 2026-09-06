"""Hold every CI collection floor to the number of tests its selection collects.

A job in ``.github/workflows/`` declares ``--min-selected <n>``: the number of
tests it expects its own ``-m`` expression to match. Section 10.3 of
``.system_design/TEST_SUITE.md`` explains why the floor exists at all -- a
selector matching *nothing* exits 5 and fails the job, but a selector matching a
handful exits 0 and the job is green having run almost nothing, and only a
declared count catches that.

**This module exists because the maintenance rule for that count failed in the
pull request that wrote it down.** Two branches merged twenty-eight seconds
apart on 2026-09-06: the one adding forty tests went first, and the one
declaring the floor went second carrying a number read from the tree *before*
that merge. Neither was wrong on its own. A count calibrated in one branch is
invalidated by any other branch that merges first, and nothing rechecked it at
merge time -- so the gate went live forty tests short, and two further merges the
same evening widened the gap to seventy-three before anyone measured it.

🔴 **The declared number is held to EQUALITY, not to "at least".** That is the
mechanism rather than a detail: it forces every pull request that changes the
collected count to edit one line of a workflow, so two branches that both change
it collide in git and a person resolves the conflict. A bounded slack would let
the same race through for any two branches that fit inside the bound.

⚠️ **Run-time semantics are unchanged.** ``pytest_collection_finish`` in
``tests/conftest.py`` still fires only when fewer tests are selected than
declared *and the run has not already failed* -- that stand-down is why an
import break keeps its own diagnosis instead of being blamed on the selector.
``--min-selected`` remains a floor when pytest reads it; only the *declared*
value is held to equality, and only here.

⚠️ **The count is read from the probe plugin, never from pytest's summary line.**
Measured on 2026-09-06: with nothing deselected pytest printed ``860 tests
collected``; with anything deselected it printed ``857/860 tests collected (3
deselected)``, and a ``(\\d+) tests collected`` parse takes the pre-deselection
number of the two. Those readings are of the day and are not the current count.
The first deselection in the broad selection arrives with the browser tests --
exactly the step this guard exists to help -- so
:func:`test_the_count_is_not_read_from_the_summary_line` drives a deselecting
selection today rather than waiting for one.

🔴 **Every check refuses what it cannot resolve rather than skipping it.** A
guard that quietly stops checking is what this module was written to replace, so
an unrecorded workflow, a ``run:`` step whose body cannot be read, a command that
is not a pytest invocation and an option the parser does not model are each an
error naming the thing, not a silent pass.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

#: The workflow files that declare a collection floor. Recorded rather than
#: discovered, because a set derived only from the sweep is satisfied by a sweep
#: that finds nothing -- a renamed file or a reflowed block scalar would empty
#: the loop and every check below would pass having measured no job at all. A
#: file added here is a file whose selection this guard must be able to collect;
#: that is the question its author is meant to confront.
FLOOR_BEARING_WORKFLOWS = frozenset({"tests-broad.yml"})

OPTION = "--min-selected"

#: The invocation every floor-bearing command must open with. Anything else is
#: refused: this guard reproduces the command in a child, and it can only do that
#: for a shape it understands.
PYTEST_INVOCATION = ["python", "-m", "pytest"]

#: The plugin that reports the collected node ids. Shared with
#: :mod:`tests.test_baseline_failure_ledger`, which is why it is a plugin rather
#: than a hook copied into each consumer.
PROBE_PLUGIN = "tests._baseline_probe"

#: Both extensions GitHub honours for a workflow file. Globbing only ``*.yml``
#: would hide a floor declared in a ``.yaml`` file from the sweep, and the
#: recorded-set comparison cannot see a file it never visited.
WORKFLOW_SUFFIXES = ("*.yml", "*.yaml")

CHILD_TIMEOUT_SECONDS = 300

STEPS_KEY = re.compile(r"^(?P<indent> *)steps: *$")
STEP_ITEM = re.compile(r"^(?P<indent> *)- ")
RUN_KEY = re.compile(
    r"^(?P<indent> *)(?P<dash>- )?run: *(?P<style>[|>][-+]?)? *(?P<inline>.*)$"
)


def _indent_of(line: str) -> int:
    """Return a line's indentation depth.

    Args:
        line: The line to measure.

    Returns:
        The number of leading spaces.
    """

    return len(line) - len(line.lstrip())


def _run_commands(text: str) -> list[str]:
    """Extract every shell command a workflow's ``run:`` steps execute.

    🔴 **Only a ``run:`` that is a step's own key counts.** An action input may
    legally be named ``run`` under ``with:``, and reading one as a command
    invents a job -- with a floor nothing enforces -- out of an input value. The
    sweep therefore tracks the ``steps:`` block and the column at which the
    current step's keys sit, and accepts ``run:`` only at that column.

    A literal block (``|``) is several commands and is returned as several, so a
    step that installs on one line and runs pytest on the next is read as the two
    things it is. Backslash continuations inside such a block are spliced first,
    because that is the ordinary way to wrap a long command and a parser that
    kept the newline would refuse it while naming the wrong cause. A folded block
    (``>``) is one command however its author wrapped it.

    Args:
        text: The workflow file's contents.

    Returns:
        One string per command, in file order.

    Raises:
        ValueError: When a ``run:`` step's body is in a shape this parser cannot
            read -- a plain multi-line scalar, which would otherwise be returned
            as an empty string and lose whatever it declared, silently.
    """

    commands: list[str] = []
    lines = text.splitlines()
    steps_indent: int | None = None
    step_key_indent: int | None = None
    index = 0
    while index < len(lines):
        line = lines[index]

        # Entering, and leaving, the block whose children are steps.
        steps = STEPS_KEY.match(line)
        if steps is not None:
            steps_indent, step_key_indent = len(steps.group("indent")), None
            index += 1
            continue
        # Two shapes are structural no-ops and must not end the region. A
        # comment, wherever it sits: one at column 0 between two steps would hide
        # every floor below it. And a step item at the `steps:` key's own indent,
        # because YAML allows an indentless sequence -- verified against a real
        # parser, which reads two steps from a block this scan used to read as
        # none. Silent truncation is the failure this module exists to refuse
        # rather than commit, so the item is matched before the region is closed.
        item = STEP_ITEM.match(line)
        if (
            steps_indent is not None
            and line.strip()
            and not line.lstrip().startswith("#")
            and (item is None or len(item.group("indent")) < steps_indent)
            and _indent_of(line) <= steps_indent
        ):
            steps_indent = step_key_indent = None

        if (
            steps_indent is not None
            and item is not None
            and len(item.group("indent")) >= steps_indent
        ):
            step_key_indent = len(item.group("indent")) + 2

        run = RUN_KEY.match(line)
        if run is None or step_key_indent is None:
            index += 1
            continue
        # A dashed key sits two columns left of the block it introduces.
        if (
            len(run.group("indent")) + (2 if run.group("dash") else 0)
            != step_key_indent
        ):
            index += 1
            continue

        style, inline = run.group("style"), run.group("inline").strip()
        if not style:
            if not inline:
                raise ValueError(
                    f"line {index + 1}: a `run:` step gives neither a block style "
                    f"(`|` or `>`) nor an inline command. This parser cannot read "
                    f"a plain multi-line scalar, and returning it empty would lose "
                    f"whatever it declares."
                )
            commands.append(inline)
            index += 1
            continue

        body: list[str] = []
        index += 1
        while index < len(lines):
            if lines[index].strip() and _indent_of(lines[index]) <= step_key_indent:
                break
            body.append(lines[index].strip())
            index += 1
        kept = [part for part in body if part]
        if style.startswith(">"):
            commands.append(" ".join(kept))
        else:
            spliced = "\n".join(kept).replace("\\\n", " ")
            commands.extend(part for part in spliced.splitlines() if part.strip())
    return commands


def _refuse_an_unresolvable_command(argv: list[str], command: str) -> None:
    """Reject a floor-bearing command this module cannot reproduce.

    Two shapes are refused. One is a command that is not a direct pytest
    invocation -- a floor inside ``bash -c "..."`` is a floor this guard would
    have to guess at. The other is an invocation carrying an option the parser
    does not model: those are perfectly valid pytest, and a parser that ignored
    them would report agreement while collecting a different set of tests than
    the job does.

    Args:
        argv: The command, split into tokens.
        command: The command as written, quoted into the message.

    Raises:
        ValueError: When the command is not a pytest invocation, or carries an
            option outside the modelled set, or declares a floor that is not an
            integer.
    """

    if argv[: len(PYTEST_INVOCATION)] != PYTEST_INVOCATION:
        raise ValueError(
            f"a collection floor is declared on a command that must open with "
            f"{' '.join(PYTEST_INVOCATION)!r} and does not: {command!r}. If this "
            f"command does not in fact declare a floor, spell the option so it "
            f"does not appear here."
        )

    # Walked rather than pattern-matched: the point is to name the first token
    # that is not modelled, not merely to notice that one exists.
    position = len(PYTEST_INVOCATION)
    while position < len(argv):
        token = argv[position]
        if token.startswith("--ignore="):
            position += 1
        elif token in {"-m", OPTION}:
            if position + 1 >= len(argv):
                raise ValueError(f"{token!r} carries no value in {command!r}")
            if token == OPTION and not argv[position + 1].isdigit():
                raise ValueError(f"{OPTION} is not an integer in {command!r}")
            position += 2
        else:
            raise ValueError(
                f"this guard does not model the option {token!r}, in {command!r}. "
                f"Teach the parser what it does to collection before declaring a "
                f"floor beside it."
            )


def _declared_floors(text: str) -> list[tuple[list[str], int]]:
    """Recover every collection floor a workflow file declares.

    Only ``run:`` steps are read. A comment mentioning the option is prose, not a
    declaration, and reading one as a declaration would hold the tree to a number
    nothing enforces.

    Args:
        text: The workflow file's contents.

    Returns:
        One ``(argv, floor)`` pair per declaration, in file order.

    Raises:
        ValueError: Through :func:`_run_commands` or
            :func:`_refuse_an_unresolvable_command`.
    """

    floors: list[tuple[list[str], int]] = []
    for command in _run_commands(text):
        if OPTION not in command:
            continue
        argv = shlex.split(command)
        _refuse_an_unresolvable_command(argv, command)
        floors.append((argv, int(argv[argv.index(OPTION) + 1])))
    return floors


def _workflow_files(directory: Path) -> list[Path]:
    """Return every workflow file in a directory, both extensions.

    Args:
        directory: The directory to list.

    Returns:
        The files, sorted by name.
    """

    return sorted(
        {path for suffix in WORKFLOW_SUFFIXES for path in directory.glob(suffix)}
    )


def _floor_bearing_workflows(directory: Path) -> set[str]:
    """Return the names of the workflow files that declare a floor.

    Args:
        directory: The directory to sweep.

    Returns:
        File names, without their directory.
    """

    return {
        path.name
        for path in _workflow_files(directory)
        if _declared_floors(path.read_text(encoding="utf-8"))
    }


def _selection_options(argv: list[str]) -> list[str]:
    """Strip the declared floor from a job's invocation, keeping the selection.

    Args:
        argv: The workflow command, split into tokens.

    Returns:
        Everything after ``python -m pytest`` except ``--min-selected`` and its
        value.
    """

    options: list[str] = []
    position = len(PYTEST_INVOCATION)
    while position < len(argv):
        if argv[position] == OPTION:
            position += 2
            continue
        options.append(argv[position])
        position += 1
    return options


def _child_command(argv: list[str], probe_path: Path) -> list[str]:
    """Build the argv that collects one job's selection.

    🔴 ``--collect-only`` is an invariant, not a speed optimisation.
    :mod:`tests.test_baseline_failure_ledger` runs the whole suite in a child and
    excludes only itself, so the behavioural case below runs *inside* that child.
    A version of this command that ran the selection instead of collecting it
    would make that child spawn a whole-suite grandchild for every declared
    floor.

    ``-c`` and the working directory are load-bearing too: without them
    ``testpaths`` and rootdir discovery would decide what the child collects, and
    the answer would depend on where the parent happened to be started.

    Args:
        argv: The workflow command, split into tokens.
        probe_path: Where the probe plugin writes its JSON result.

    Returns:
        The full command line, this interpreter first. The job's own selection
        is last, contiguously, so a check can compare the tail.
    """

    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        PROBE_PLUGIN,
        "-c",
        str(PYPROJECT_PATH),
        f"--baseline-probe-json={probe_path}",
        "--collect-only",
        "-q",
        *_selection_options(argv),
    ]


def _render_command(
    tokens: list[str], executable: str, pyproject: Path, probe_path: Path
) -> str:
    """Render a child argv with the machine-specific tokens replaced.

    🔴 **Substituted token by token, then joined -- not joined and then
    substituted.** On Windows all three varying tokens contain backslashes, so
    :func:`shlex.quote` wraps each in single quotes; replacing the inner text
    afterwards leaves those quotes behind and the pinned string differs by
    platform. The check would then be red on one leg of the matrix and green on
    the other, which is worse than either.

    ⚠️ **The varying values are parameters rather than module globals read
    directly.** A test that reached them by name would have to spell one of those
    names as a string, and ``test_baseline_failure_ledger.py`` reads every
    upper-case underscored string literal under ``tests/`` as an environment
    variable its child must clear -- so a check written that way fails a test in
    another module, with a message about the environment.

    Args:
        tokens: The child argv.
        executable: The interpreter to replace with ``python``.
        pyproject: The configuration path to replace with a placeholder.
        probe_path: The probe output path to replace with a placeholder.

    Returns:
        One line, suitable for comparing against a literal.
    """

    placeholders = {
        executable: "python",
        str(pyproject): "<repo>/pyproject.toml",
        f"--baseline-probe-json={probe_path}": "--baseline-probe-json=<tempfile>",
    }
    # A placeholder is emitted verbatim; only the real tokens are quoted. Quoting
    # the placeholders instead would wrap `<repo>/...` in quotes of its own,
    # since the angle brackets are shell metacharacters.
    return " ".join(placeholders.get(token) or shlex.quote(token) for token in tokens)


def _rendered_child_command(argv: list[str], probe_path: Path) -> str:
    """Render this machine's child argv for one job.

    Args:
        argv: The workflow command, split into tokens.
        probe_path: The path used in this invocation.

    Returns:
        The rendered line.
    """

    return _render_command(
        _child_command(argv, probe_path), sys.executable, PYPROJECT_PATH, probe_path
    )


def _probe_count(probe_path: Path, output: str) -> int:
    """Read the collected count the probe plugin recorded.

    Args:
        probe_path: The file the child was told to write.
        output: The child's combined output, quoted into the failure message.

    Returns:
        The number of node ids the selection collected, after deselection.

    Raises:
        Failed: When the child wrote no probe output. That means the plugin never
            loaded, which is indistinguishable from a correct count unless it is
            named -- and letting :meth:`Path.read_text` raise instead would
            report a missing file rather than a disarmed guard.
    """

    if not probe_path.is_file():
        pytest.fail(
            f"The child wrote no probe output to {probe_path}, so the "
            f"{PROBE_PLUGIN!r} plugin did not load and this guard has nothing to "
            f"compare.\n{output}"
        )
    return len(json.loads(probe_path.read_text(encoding="utf-8"))["collected"])


def _collect(argv: list[str], probe_path: Path) -> tuple[int, str]:
    """Collect one job's selection in a child process.

    Args:
        argv: The workflow command, split into tokens.
        probe_path: Where the probe plugin writes its JSON result.

    Returns:
        The collected count and the child's standard output.

    Raises:
        Failed: When the child does not finish in time, or wrote no probe output.
    """

    try:
        child = subprocess.run(
            _child_command(argv, probe_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(
            f"Collecting the selection did not finish within "
            f"{CHILD_TIMEOUT_SECONDS}s; it normally takes about two. Partial "
            f"output:\n{expired.stdout or ''}\n{expired.stderr or ''}"
        )
    return _probe_count(probe_path, f"{child.stdout}\n{child.stderr}"), child.stdout


def _floor_mismatch_message(
    name: str, argv: list[str], declared: int, collected: int
) -> str:
    """Compose the failure a stale floor produces.

    Factored out so that what a maintainer reads at 3am is itself asserted. This
    guard is marked ``subsystem``, so it is deselected from the fast lane a
    developer runs locally and the message normally arrives from a CI job.

    Args:
        name: The workflow file that declares the floor.
        argv: The workflow command, split into tokens.
        declared: The number the workflow states.
        collected: The number its selection actually collects.

    Returns:
        The message, naming both numbers and the line to write.
    """

    return (
        f"{name} declares `{OPTION} {declared}` for the selection "
        f"{shlex.join(_selection_options(argv))!r}, which collects {collected}. "
        f"Read the count from a run, then write `{OPTION} {collected}` in "
        f".github/workflows/{name}."
    )


ONE_FLOOR = """\
jobs:
  a:
    steps:
      - name: Select
        run: >-
          python -m pytest
          --ignore=tests/package
          -m "not live"
          --min-selected 12
"""

TWO_FLOORS = (
    ONE_FLOOR
    + """\
      - name: Select again
        run: >-
          python -m pytest -m "subsystem" --min-selected 34
"""
)

#: One specimen per ``run:`` shape that appears in real workflow files, each
#: declaring the same floor. Written as a table because the parser's indent
#: arithmetic and its style branch were, before this existed, driven at one
#: indentation and for two shapes -- and four of these were mis-read.
RUN_SHAPES = {
    "folded, wrapped at a different column": """\
jobs:
  a:
    steps:
      - name: Select
        run: >-
            python -m pytest
              -m "not live" --min-selected 12
""",
    "literal block": """\
jobs:
  a:
    steps:
      - name: Select
        run: |
          python -m pytest -m "not live" --min-selected 12
""",
    "literal block with a chomp indicator": """\
jobs:
  a:
    steps:
      - run: |-
          python -m pytest -m "not live" --min-selected 12
""",
    "literal block with backslash continuations": """\
jobs:
  a:
    steps:
      - run: |
          python -m pytest \\
            -m "not live" \\
            --min-selected 12
""",
    "literal block whose other lines are not pytest": """\
jobs:
  a:
    steps:
      - run: |
          python -m pip install -e ".[dev]"
          python -m pytest -m "not live" --min-selected 12
""",
    "inline on a dashed key": """\
jobs:
  a:
    steps:
      - run: python -m pytest -m "not live" --min-selected 12
""",
    "an indentless sequence, the item at the steps key's own indent": """\
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
    - run: python -m pytest -m "not live" --min-selected 12
""",
    "a deeply indented job": """\
on: [push]
jobs:
  outer:
    steps:
      - name: Select
        run: >-
          python -m pytest -m "not live" --min-selected 12
""",
}

NOT_A_PYTEST_INVOCATION = """\
jobs:
  a:
    steps:
      - run: bash -c "pytest --min-selected 5"
"""

AN_UNMODELLED_OPTION = """\
jobs:
  a:
    steps:
      - run: python -m pytest -k something --min-selected 5
"""

ONLY_IN_A_COMMENT = """\
jobs:
  a:
    steps:
      # --min-selected 42 is the floor
      - run: python -m pytest -m "fast"
"""

A_COMMENT_BETWEEN_STEPS = """\
jobs:
  a:
    steps:
      - run: python -m pytest -m "not live" --min-selected 12
# A comment is a structural no-op wherever it sits, column 0 included.
      - run: python -m pytest -m "subsystem" --min-selected 34
"""

AN_ACTION_INPUT_NAMED_RUN = """\
jobs:
  a:
    steps:
      - uses: some/action@v1
        with:
          run: python -m pytest --min-selected 99
"""

A_PLAIN_MULTI_LINE_SCALAR = """\
jobs:
  a:
    steps:
      - run:
          python -m pytest --min-selected 7
"""


def test_a_folded_command_is_recovered_whole() -> None:
    """A folded block scalar is one command, wrapped wherever its author liked.

    Recovering it line by line would read ``--min-selected`` and its value as
    separate steps, and pin the selection to whichever fragment happened to carry
    the option.
    """

    ((argv, floor),) = _declared_floors(ONE_FLOOR)

    assert floor == 12
    assert argv == [
        "python",
        "-m",
        "pytest",
        "--ignore=tests/package",
        "-m",
        "not live",
        "--min-selected",
        "12",
    ]


@pytest.mark.parametrize("shape", sorted(RUN_SHAPES))
def test_every_run_shape_a_workflow_may_use_is_read(shape: str) -> None:
    """One case per shape, because the parser is indent arithmetic.

    Each specimen declares the same floor by a different spelling. Before this
    table existed the parser read a ``run:`` at exactly two indentations and in
    two styles; a literal block lost its backslash continuations, and a block
    whose first line was an install refused the whole step.

    Args:
        shape: The key naming the specimen under test.
    """

    floors = _declared_floors(RUN_SHAPES[shape])

    assert [floor for _, floor in floors] == [12], f"{shape} was not read"
    assert floors[0][0][: len(PYTEST_INVOCATION)] == PYTEST_INVOCATION


def test_two_floors_in_one_file_are_both_recovered() -> None:
    """One file, two jobs, two numbers -- and the older guard sees only the first.

    ``BroadTestJobWiringTests`` finds the floor with a single ``re.search``, so
    it reads the first declaration in a file and no other. The step that splits
    the broad job introduces the second, which is when a parser that stops at one
    starts reporting agreement it never checked.
    """

    assert [floor for _, floor in _declared_floors(TWO_FLOORS)] == [12, 34]


def test_a_comment_between_steps_does_not_end_the_scan() -> None:
    """Measured: a comment at column 0 hid the second floor entirely.

    The region ended on the first non-blank line indented no further than
    ``steps:``, and a comment is such a line. The floor below it was not
    refused and not reported -- it simply was not there, and the recorded-set
    check could not see it either, because that compares only *which files*
    declare at least one floor.
    """

    assert [floor for _, floor in _declared_floors(A_COMMENT_BETWEEN_STEPS)] == [12, 34]


def test_an_action_input_named_run_is_not_a_command() -> None:
    """``run`` is a legal input name under ``with:``.

    Read as a step's command it invents a job, with a floor nothing enforces, out
    of an input value -- and the invented floor would then have to agree with the
    tree, which is a failure with no correct fix.
    """

    assert _declared_floors(AN_ACTION_INPUT_NAMED_RUN) == []


def test_a_run_body_this_parser_cannot_read_is_refused() -> None:
    """The one shape that would be lost *silently* rather than loudly.

    A plain multi-line scalar returns an empty command, so a floor declared in
    one disappears from the sweep and every check here passes without it.
    """

    with pytest.raises(ValueError, match="plain multi-line scalar"):
        _declared_floors(A_PLAIN_MULTI_LINE_SCALAR)


def test_a_command_that_is_not_a_pytest_invocation_is_refused() -> None:
    """Refused and named, not skipped. A skip here is a guard that stopped.

    Matched on the phrase unique to this refusal, not on the offending token:
    every refusal quotes the whole command, so matching ``bash`` would be
    satisfied by any refusal at all.
    """

    with pytest.raises(ValueError, match="must open with") as refusal:
        _declared_floors(NOT_A_PYTEST_INVOCATION)

    assert "bash" in str(refusal.value)


def test_an_option_the_parser_does_not_model_is_refused() -> None:
    """``-k`` is valid pytest and changes what is collected.

    This is the half-measure worth guarding against: the command resolves, the
    parser reproduces it minus the option it never understood, and the two counts
    agree on different sets of tests.
    """

    with pytest.raises(ValueError, match="does not model") as refusal:
        _declared_floors(AN_UNMODELLED_OPTION)

    assert "'-k'" in str(refusal.value), "the refusal must name the offending option"


def test_a_comment_mentioning_the_option_is_not_a_declaration() -> None:
    """The workflow's own prose says ``--min-selected`` several times.

    Reading prose as a declaration would hold the tree to a number that no job
    passes to pytest.
    """

    assert _declared_floors(ONLY_IN_A_COMMENT) == []


def test_every_workflow_that_declares_a_floor_is_recorded() -> None:
    """Pin the SET, in both directions, and refuse an empty sweep.

    A sweep that found nothing would satisfy every other check in this module by
    running none of them. Equality against a recorded set catches a floor added
    in a file nobody taught this guard about, and a recorded file that stopped
    declaring one.
    """

    found = _floor_bearing_workflows(WORKFLOW_DIR)

    assert found, "the sweep found no declared floor at all, so it measured nothing"
    assert found == FLOOR_BEARING_WORKFLOWS, (
        "the set of floor-declaring workflows moved; a new one must be recorded "
        "in FLOOR_BEARING_WORKFLOWS, which means deciding whether this guard can "
        "collect its selection"
    )


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_the_sweep_sees_a_floor_in_a_file_it_was_never_told_about(
    suffix: str, tmp_path: Path
) -> None:
    """The sweep is a walk, not a lookup of the recorded names.

    Written the other way round -- iterate the record, read those files -- a
    later job's floor would be invisible rather than reported, and the check
    above could never fail in the direction that matters. Both suffixes are
    driven because GitHub honours both and a ``.yaml`` file globbed away is a
    hole the recorded-set comparison cannot see.

    Args:
        suffix: The workflow file extension under test.
        tmp_path: A directory standing in for ``.github/workflows``.
    """

    (tmp_path / f"later{suffix}").write_text(ONE_FLOOR, encoding="utf-8")

    assert _floor_bearing_workflows(tmp_path) == {f"later{suffix}"}


def test_the_child_reproduces_the_selection_without_the_floor() -> None:
    """Pinned in rendered form, so an option added to the child is visible.

    The child must collect the same tests the job runs. Every token here earns
    its place: the two plugins, the pinned configuration file, and the selection
    copied from the workflow -- with ``--min-selected`` removed, because passing
    the number back in would make the child enforce the very floor under test.
    """

    ((argv, _),) = _declared_floors(ONE_FLOOR)

    assert _rendered_child_command(argv, Path("/tmp/probe.json")) == (
        "python -m pytest -p no:cacheprovider -p tests._baseline_probe "
        "-c <repo>/pyproject.toml --baseline-probe-json=<tempfile> "
        "--collect-only -q --ignore=tests/package -m 'not live'"
    )


def test_the_rendered_pin_is_the_same_on_a_windows_shaped_path() -> None:
    """The pin above is unmarked, so it runs on both legs of the matrix.

    ``shlex.quote`` wraps a token containing a backslash in single quotes.
    Rendering by joining first and substituting afterwards therefore left those
    quotes around every placeholder on Windows, and the pin was red on one leg
    and green on the other -- a failure this repository has no Windows lane to
    show a developer.

    The token list is written out rather than derived, so that what is being
    rendered is visible. Its length is held to the real child's, so a token added
    there cannot leave this specimen quietly stale.
    """

    executable = r"C:\Python313\python.exe"
    pyproject = Path(r"C:\repo\pyproject.toml")
    probe_path = Path(r"C:\Users\runneradmin\probe.json")
    tokens = [
        executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        PROBE_PLUGIN,
        "-c",
        str(pyproject),
        f"--baseline-probe-json={probe_path}",
        "--collect-only",
        "-q",
        "--ignore=tests/package",
        "-m",
        "not live",
    ]

    ((argv, _),) = _declared_floors(ONE_FLOOR)
    assert len(tokens) == len(_child_command(argv, Path("/tmp/probe.json"))), (
        "this specimen no longer has the same shape as the command it stands in "
        "for; update it alongside _child_command"
    )
    assert _render_command(tokens, executable, pyproject, probe_path) == (
        "python -m pytest -p no:cacheprovider -p tests._baseline_probe "
        "-c <repo>/pyproject.toml --baseline-probe-json=<tempfile> "
        "--collect-only -q --ignore=tests/package -m 'not live'"
    )


def test_the_child_collects_and_never_runs() -> None:
    """Separately from the pin above, because this one is a fork bomb if it goes.

    The ledger's whole-suite child runs this module, so a child that ran its
    selection instead of collecting it would spawn a whole-suite grandchild per
    declared floor.
    """

    ((argv, _),) = _declared_floors(ONE_FLOOR)

    assert "--collect-only" in _child_command(argv, Path("/tmp/probe.json"))


def test_the_job_selection_reaches_the_child_intact() -> None:
    """Compared as a contiguous tail, not by membership.

    Membership cannot see ``-m`` being dropped -- ``python -m pytest`` already
    puts a ``-m`` in the child -- and it cannot see the options being reordered
    into a different selection. The tail comparison sees both. Asserted
    non-vacuous first: over an empty list every membership check passes having
    compared nothing, which is how an earlier version of this case read green
    against a stub.
    """

    floors = _declared_floors(
        (WORKFLOW_DIR / "tests-broad.yml").read_text(encoding="utf-8")
    )

    assert floors, "the broad job declares no floor, so this proves nothing"
    for argv, _ in floors:
        expected = _selection_options(argv)
        assert expected, "the job declares no selection options, so this proves nothing"
        assert (
            _child_command(argv, Path("/tmp/probe.json"))[-len(expected) :] == expected
        )


def test_the_mismatch_message_names_both_numbers_and_the_line_to_write() -> None:
    """What a maintainer reads is itself a claim, so it is asserted.

    This guard is deselected from the fast lane, so the message normally arrives
    from a CI job ten minutes in. A message that named only "a mismatch" would
    cost a local re-run to find out which number to write.
    """

    ((argv, _),) = _declared_floors(ONE_FLOOR)

    message = _floor_mismatch_message("tests-broad.yml", argv, 787, 871)

    assert "tests-broad.yml" in message
    assert "787" in message and "871" in message
    assert f"{OPTION} 871" in message
    assert "not live" in message, "the message must name which selection disagreed"


def test_a_child_that_wrote_no_probe_output_is_a_failure(tmp_path: Path) -> None:
    """A missing probe file is a disarmed guard, and must read as one.

    Without this branch the count would be read straight from the file and the
    run would die on a missing path -- a diagnosis about the filesystem, on a
    guard whose actual problem is that its plugin never loaded.

    Args:
        tmp_path: A directory holding no probe output.
    """

    with pytest.raises(pytest.fail.Exception, match="did not load"):
        _probe_count(tmp_path / "absent.json", "the child's output")


@pytest.mark.subsystem
@pytest.mark.slow
def test_the_count_is_not_read_from_the_summary_line(tmp_path: Path) -> None:
    """🔴 The mutation that survived every other case in this module.

    Under deselection pytest's summary prints two numbers -- ``857/860 tests
    collected (3 deselected)`` -- and the obvious regex takes the second, the
    pre-deselection total. Every other case here passes with the count read that
    way, because the broad selection deselects nothing today. So this case drives
    a selection that *does* deselect, and pins the returned count to the first
    number while requiring it to differ from the second.

    Args:
        tmp_path: Where the child writes its probe output.
    """

    argv = shlex.split(
        "python -m pytest --ignore=tests/package "
        '-m "not live and not chromium and not package and not subsystem" '
        f"{OPTION} 1"
    )

    count, stdout = _collect(argv, tmp_path / "deselecting.json")

    summary = re.search(r"(\d+)/(\d+) tests collected \((\d+) deselected\)", stdout)
    assert summary is not None, (
        "this case needs a selection that deselects something; pytest printed:\n"
        f"{stdout[-400:]}"
    )
    assert int(summary.group(3)) > 0
    assert count == int(summary.group(1))
    assert count != int(summary.group(2)), (
        "the summary's second number is the pre-deselection total, which is what "
        "a count parsed from stdout would take"
    )


@pytest.mark.subsystem
@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(FLOOR_BEARING_WORKFLOWS))
def test_every_declared_floor_matches_what_its_selection_collects(
    name: str, tmp_path: Path
) -> None:
    """The check that does the real work: the number against the tree.

    Equality in both directions. A ``>=`` here would accept the defect this
    module was written for -- a floor forty tests below its selection when it
    merged and seventy-three by the time anyone measured, which is room for a
    whole subsystem to leave the merge gate unnoticed.

    Args:
        name: The workflow file to check.
        tmp_path: Where each child writes its probe output.
    """

    floors = _declared_floors((WORKFLOW_DIR / name).read_text(encoding="utf-8"))

    assert floors, f"{name} is recorded as declaring a floor and declares none"
    for index, (argv, declared) in enumerate(floors):
        collected, _ = _collect(argv, tmp_path / f"{index}.json")
        assert collected == declared, _floor_mismatch_message(
            name, argv, declared, collected
        )
