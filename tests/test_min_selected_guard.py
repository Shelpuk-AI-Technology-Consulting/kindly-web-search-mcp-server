"""Guard the ``--min-selected`` collection floor declared in ``tests/conftest.py``.

Section 10.3 of ``.system_design/TEST_SUITE.md`` is the policy; the
``pytest_addoption`` and ``pytest_collection_finish`` hooks in
:mod:`tests.conftest` are what the runner actually executes. This module keeps
the two in step, in the same guard shape as
:mod:`tests.test_pytest_configuration`.

The floor exists because ``strict_markers`` cannot see the failure it addresses.
Strict markers reject an *unknown* mark applied to a test; they say nothing about
a CI job whose ``-m`` expression selects two tests when it should select forty.
Measured on pytest 9.1.1, a selector matching *nothing* exits 5 and fails the
job, but a selector matching *too few* exits 0 and the job is green while most of
its tests never ran. That is the silent case, and only a declared floor catches
it.

Two things about this module's assertions are deliberate and load-bearing:

* **Every child-process case asserts a message, never an exit code alone.**
  pytest returns 4 for an unrecognized argument as readily as for a
  :class:`pytest.UsageError`, so ``exit == 4`` is satisfied by a repository where
  the option was never added at all. Each such case therefore also asserts that
  ``unrecognized arguments`` is absent.
* **The design document is compared behaviourally, not textually.** Section
  10.3's snippet is executed and exercised against the same inputs as the shipped
  hooks, so a snippet that merely looks similar cannot pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests import conftest as shipped
from tests.test_pytest_configuration import _section_body

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TEST_SUITE_PATH = REPO_ROOT / ".system_design" / "TEST_SUITE.md"

DESIGN_SECTION_HEADING = "### 10.3 CI"

# The first line of section 10.3's ``--min-selected`` code block. Section 10.3 is
# the CI section and already holds several fenced blocks -- two YAML samples and
# this one -- so "the only block in the section" is the wrong selector here, even
# though it is correct for section 10.5. Anchoring on the path comment keeps the
# comparison pointed at this snippet as the section grows.
DESIGN_SNIPPET_ANCHOR = "# tests/conftest.py"

OPTION = "--min-selected"

# A marker name no test carries and none ever will. Used to drive the selected
# count to zero while the collected count stays positive.
UNMATCHED_MARKER = "no_test_carries_this_marker"

# The module the child-process cases collect. One named module rather than the
# whole of ``tests/``: the claim under test is about the option and the hook, and
# a child that imports every test module would fail this guard, with a message
# about ``--min-selected``, whenever any unrelated module broke at import.
CHILD_TARGET = Path("tests") / "test_pytest_configuration.py"

# ``pytest.ExitCode`` values, spelled out because the distinctions matter and a
# bare integer in an assertion hides them.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2
EXIT_USAGE_ERROR = 4
EXIT_NO_TESTS_COLLECTED = 5

# What pytest prints when an option was never registered. Its *absence* is what
# makes an exit-4 assertion mean "the guard fired" rather than "the flag does not
# exist", so it is asserted on every case that expects 4 from the guard.
UNRECOGNIZED = "unrecognized arguments"


class _FakeConfig:
    """Stand-in for :class:`pytest.Config` exposing one option.

    The hook reads exactly one thing from the configuration, so the fake offers
    exactly that. Anything wider would let a change to the hook's dependencies
    pass unnoticed.
    """

    def __init__(self, minimum: int) -> None:
        """Record the value ``getoption`` will return.

        Args:
            minimum: The value to report for ``--min-selected``.
        """
        self._minimum = minimum

    def getoption(self, name: str) -> int:
        """Return the recorded value for ``--min-selected``.

        Args:
            name: The option being read.

        Returns:
            The minimum this fake was built with.

        Raises:
            AssertionError: When the hook reads any option other than
                ``--min-selected``, which would mean it had grown a dependency
                this fake does not model.
        """
        assert name == OPTION, f"hook read unexpected option {name!r}"
        return self._minimum


class _FakeSession:
    """Stand-in for :class:`pytest.Session` carrying the three fields the hook reads.

    ``items`` is the post-deselection selected list, ``testsfailed`` is pytest's
    running failure count, and ``config`` supplies the floor.
    """

    def __init__(self, *, selected: int, minimum: int, testsfailed: int = 0) -> None:
        """Build a session in a given collection state.

        Args:
            selected: How many items survived deselection. The items themselves
                are never inspected by the hook, only counted.
            minimum: The value of ``--min-selected``.
            testsfailed: pytest's failure count at collection time; non-zero when
                a module failed to import.
        """
        self.items: list[object] = [object()] * selected
        self.testsfailed = testsfailed
        self.config = _FakeConfig(minimum)


class _RecordingParser:
    """Stand-in for pytest's option parser that records the registration call.

    Used to compare the shipped ``pytest_addoption`` with the design document's
    without depending on pytest's internal parser state.
    """

    def __init__(self) -> None:
        """Start with no recorded call."""
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def addoption(self, *args: Any, **kwargs: Any) -> None:
        """Record one option registration.

        Args:
            *args: Positional arguments, in practice the option string.
            **kwargs: Keyword arguments such as ``type``, ``default`` and
                ``help``.
        """
        self.calls.append((args, kwargs))


def test_guard_raises_when_selection_is_below_the_minimum() -> None:
    """Assert an under-selected run is rejected, naming both counts

    The message is asserted along with the raise because the message *is* the
    feature: an engineer meeting this in CI has a green-looking selector and no
    other clue. "selected 1 tests, expected at least 5" plus a pointer at the
    ``-m`` expression is the whole diagnosis.
    """
    session = _FakeSession(selected=1, minimum=5)

    with pytest.raises(pytest.UsageError) as raised:
        shipped.pytest_collection_finish(session)

    message = str(raised.value)
    assert "selected 1 tests" in message, message
    assert "expected at least 5" in message, message
    assert "-m" in message, message


def test_guard_accepts_exactly_the_minimum() -> None:
    """Assert the floor is inclusive -- the control for the case above

    Without this, the guard could satisfy the previous case by rejecting every
    run, and a correct selection matching its floor exactly would fail CI.
    """
    shipped.pytest_collection_finish(_FakeSession(selected=5, minimum=5))


def test_guard_is_inert_at_the_default_of_zero() -> None:
    """Assert the default disables the guard, even with nothing selected

    Every existing invocation in this repository, and every developer running
    ``pytest`` by hand, runs without the option. A guard that fired at its
    default would break all of them, so the disabled state is asserted at its
    most extreme input: zero selected.
    """
    shipped.pytest_collection_finish(_FakeSession(selected=0, minimum=0))


def test_guard_stands_down_when_the_run_has_already_failed() -> None:
    """Assert a run that has already failed is not re-diagnosed as under-selection

    A module that fails to import lowers the selected count and leaves
    ``testsfailed`` non-zero. Without this condition the guard fires on top of
    the collection error and the headline becomes "check the -m expression" --
    blaming an innocent selector for an unrelated import break, and replacing
    pytest's own exit 2 with exit 4.

    The guard exists to turn a **green** run red. A run that is already red needs
    no help and loses nothing by the guard staying quiet.
    """
    shipped.pytest_collection_finish(
        _FakeSession(selected=1, minimum=5, testsfailed=1)
    )


def test_option_default_is_zero(pytestconfig: pytest.Config) -> None:
    """Assert the option is registered in the **running** pytest, defaulting to 0

    Read through ``pytestconfig`` rather than from the source: this is the case
    that notices if ``tests/conftest.py`` stops being an initial conftest and the
    registration silently stops happening.

    Args:
        pytestconfig: The session's configuration, injected by pytest.
    """
    assert pytestconfig.getoption(OPTION) == 0, (
        f"pytest reports {OPTION} = {pytestconfig.getoption(OPTION)!r}; section "
        "10.3 of TEST_SUITE.md requires a default of 0 so that every invocation "
        "without the option is unaffected."
    )


def _design_document_snippet() -> str:
    """Extract section 10.3's ``--min-selected`` code block from ``TEST_SUITE.md``.

    The section is bounded with :func:`tests.test_pytest_configuration._section_body`
    rather than a fresh heading regex. That helper exists because the naive bound
    was measured wrong -- a ``#`` comment at column 0 inside a fence is
    indistinguishable from a heading -- and it carries its own rationale and
    tests. A second copy here would drift from it.

    Within the section the block is chosen by its first line rather than by being
    the only one: section 10.3 is the CI section and already holds two YAML
    samples besides this snippet, and the CI epic will add more.

    Returns:
        The snippet's source, ready to execute.

    Raises:
        AssertionError: When the heading, or exactly one anchored block beneath
            it, cannot be found -- either of which would silently disable this
            comparison.
    """
    text = TEST_SUITE_PATH.read_text(encoding="utf-8")
    assert DESIGN_SECTION_HEADING in text.splitlines(), (
        f"{TEST_SUITE_PATH.name} no longer contains "
        f"'{DESIGN_SECTION_HEADING}'. This guard parses that section; renaming "
        "it would silently disable the check."
    )
    section = _section_body(text, DESIGN_SECTION_HEADING)
    blocks = [
        block
        for block in re.findall(r"```python\n(.*?)```", section, re.DOTALL)
        if block.startswith(f"{DESIGN_SNIPPET_ANCHOR}\n")
    ]
    assert len(blocks) == 1, (
        f"Section 10.3 of {TEST_SUITE_PATH.name} contains {len(blocks)} python "
        f"blocks opening with '{DESIGN_SNIPPET_ANCHOR}'; this guard requires "
        "exactly one."
    )
    return blocks[0]


def _design_document_hooks() -> dict[str, Any]:
    """Execute the design document's snippet and return the names it defines.

    Executing rather than pattern-matching the source is the point of this
    comparison: a snippet that merely *looks* like the shipped hook cannot pass,
    and the option's name, its default, the arming condition and the message text
    are all checked by exercising them rather than by four separate string
    searches that each risk being satisfied by a near miss.

    Returns:
        The namespace the snippet defines.

    Raises:
        AssertionError: When the snippet does not define both hooks.
    """
    namespace: dict[str, Any] = {}
    # The executed source is a fenced block in a version-controlled design
    # document in this repository, reached through no input this test does not
    # control -- the same trust level as importing a module from the tree.
    exec(  # noqa: S102
        compile(_design_document_snippet(), str(TEST_SUITE_PATH), "exec"),
        namespace,
    )
    for hook in ("pytest_addoption", "pytest_collection_finish"):
        assert hook in namespace, (
            f"Section 10.3's snippet in {TEST_SUITE_PATH.name} no longer defines "
            f"{hook}; {shipped.__name__} does."
        )
    return namespace


def test_design_document_registers_the_same_option() -> None:
    """Assert the documented and shipped ``pytest_addoption`` calls are identical

    Compared as the recorded call -- option string, type, default and help text --
    so a document that renames the option, or changes the default that keeps
    every existing invocation unaffected, turns this red.
    """
    documented, actual = _RecordingParser(), _RecordingParser()

    _design_document_hooks()["pytest_addoption"](documented)
    shipped.pytest_addoption(actual)

    assert documented.calls == actual.calls, (
        f"Section 10.3 of {TEST_SUITE_PATH.name} registers "
        f"{documented.calls!r} but {shipped.__name__} registers "
        f"{actual.calls!r}. Change both in the same pull request."
    )


@pytest.mark.parametrize(
    ("selected", "minimum", "testsfailed"),
    [
        pytest.param(1, 5, 0, id="below-the-floor"),
        pytest.param(5, 5, 0, id="exactly-at-the-floor"),
        pytest.param(0, 0, 0, id="guard-disabled"),
        pytest.param(1, 5, 1, id="run-already-failed"),
        pytest.param(0, -1, 0, id="negative-floor"),
    ],
)
def test_design_document_hook_behaves_identically(
    selected: int, minimum: int, testsfailed: int
) -> None:
    """Assert the documented and shipped hooks agree on one collection state

    The five states are the whole behaviour: fire, do not fire at the boundary,
    do not fire when disabled, do not fire on an already-failed run, and do not
    fire on a negative floor. Where both raise, the messages are compared too --
    the message is what an engineer reads in CI, and a document whose snippet
    says something else has stopped describing what ships.

    Args:
        selected: Items surviving deselection.
        minimum: The declared floor.
        testsfailed: pytest's failure count at collection time.
    """
    documented = _design_document_hooks()["pytest_collection_finish"]

    def outcome(hook: Any) -> str | None:
        """Run one hook and reduce it to a comparable result.

        Args:
            hook: The hook to invoke.

        Returns:
            The error message, or ``None`` when the hook allowed the run.
        """
        try:
            hook(
                _FakeSession(
                    selected=selected, minimum=minimum, testsfailed=testsfailed
                )
            )
        except pytest.UsageError as error:
            return str(error)
        return None

    assert outcome(documented) == outcome(shipped.pytest_collection_finish), (
        f"Section 10.3 of {TEST_SUITE_PATH.name} and {shipped.__name__} disagree "
        f"for selected={selected}, minimum={minimum}, testsfailed={testsfailed}. "
        "Change both in the same pull request."
    )


def _child_environment() -> dict[str, str]:
    """Build the environment every child pytest runs under.

    ``PYTEST_ADDOPTS`` is dropped: inherited from the parent it would silently
    change the child's selection, which is the one thing these cases measure.

    Returns:
        A copy of the current environment, cleaned.
    """
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    # Pinned so the child's stdout is UTF-8 on every platform. Without it a
    # Windows runner encodes with the locale codec while this process decodes
    # with its own, and a message assertion becomes a question about codecs.
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _run_pytest(
    *args: str, cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a child pytest against the **committed** ``pyproject.toml``.

    ``-c`` points the child at this repository's real configuration rather than
    at a copy written for the occasion, which is the same reasoning as
    :mod:`tests.test_pytest_configuration`: a test that builds its own config
    proves only that pytest works.

    Args:
        *args: Arguments appended after the configuration flags.
        cwd: Working directory for the child.
        environment: The child's environment.

    Returns:
        The completed process, with ``stdout`` and ``stderr`` captured as text.

    Raises:
        subprocess.TimeoutExpired: When the child has not exited within 120
            seconds, so a hung child fails one case instead of the whole run.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(PYPROJECT_PATH),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=environment,
        timeout=120,
        check=False,
    )


def _run_in_repository(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a child pytest from the repository root, over the real tree.

    This shape is what proves ``tests/conftest.py`` is genuinely wired in as an
    *initial* conftest -- the one fact no in-process test can reach, since a
    registration that stopped happening would leave the source unchanged.

    Args:
        *args: Arguments for the child.

    Returns:
        The completed process.
    """
    return _run_pytest(*args, cwd=REPO_ROOT, environment=_child_environment())


def _run_over_temporary_tests(
    *args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run a child pytest over a temporary directory, loading the shipped conftest.

    ``-p tests.conftest`` loads :mod:`tests.conftest` itself as a plugin -- the
    real module, not a copy -- which is how a case that needs a *failing* or
    *un-importable* module still exercises the shipped hook. The repository tree
    contains neither, and adding one to it would break every other run.

    Two constraints, both measured, both load-bearing:

    * ``PYTHONPATH`` must carry the repository root. The child's ``sys.path[0]``
      is its own working directory, and ``-c`` sets the rootdir without adding
      anything importable. Without it the plugin import fails -- and that failure
      exits 1, the exact code one of these cases asserts as success, so the case
      would pass forever for the wrong reason.
    * The target is always given explicitly. ``testpaths`` is consulted only
      when the invocation directory *is* the rootdir, and ``-c`` puts the rootdir
      at the repository root while the child runs in a temporary directory -- so
      an omitted target would silently fall back to the invocation directory
      rather than to ``tests``. Correct either way here, but not for a reason
      worth leaving implicit.
    * The target must never lie under ``tests/``. Normal conftest discovery would
      then reach the same file that ``-p`` already loaded, and pytest raises
      ``ValueError: Plugin already registered under a different name``.

    Args:
        *args: Arguments for the child, appended after the plugin flag.
        cwd: The temporary directory holding the test modules.

    Returns:
        The completed process.
    """
    environment = _child_environment()
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return _run_pytest(
        "-p", "tests.conftest", *args, cwd=cwd, environment=environment
    )


def _write(directory: Path, name: str, body: str) -> None:
    """Write one test module into a temporary directory.

    Args:
        directory: Where to write.
        name: The module's file name.
        body: Its source, dedented before writing.
    """
    (directory / name).write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.mark.subsystem
def test_under_selection_exits_usage_error_with_the_count() -> None:
    """Assert an under-selected real run aborts with exit 4 and names the counts

    The end-to-end form of the whole feature, and the case that pins the choice
    of hook. The floor of 1 sits deliberately *between* the selected count (0,
    after deselection) and the collected count (every case in the target module).
    A ``pytest_collection_modifyitems`` implementation reads the collected count,
    would find it comfortably above 1, would not fire, and this run would exit 5
    instead.

    The message is asserted alongside the exit code, and ``unrecognized
    arguments`` asserted absent, because pytest returns 4 for an unknown flag
    too: without that second half this case is green against a repository where
    the option was never added.
    """
    result = _run_in_repository(
        "-q", "-m", UNMATCHED_MARKER, f"{OPTION}=1", str(CHILD_TARGET)
    )

    assert result.returncode == EXIT_USAGE_ERROR, (
        f"Expected exit {EXIT_USAGE_ERROR} (usage error), got "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )
    assert UNRECOGNIZED not in result.stderr, (
        f"pytest exited {EXIT_USAGE_ERROR} because {OPTION} is not registered, "
        f"not because the guard fired.\n{result.stderr}"
    )
    assert "selected 0 tests, expected at least 1" in result.stderr, (
        f"The guard fired without naming both counts.\n{result.stderr}"
    )
    assert "-m" in result.stderr, (
        f"The guard's message does not point at the -m expression.\n"
        f"{result.stderr}"
    )
    # The precondition the paragraph above depends on, asserted rather than
    # assumed. If the target module ever collects nothing, the floor no longer
    # lies between the selected and collected counts, a modifyitems
    # implementation would exit 4 here too, and this case would silently stop
    # discriminating while staying green.
    # Matched without the surrounding parentheses: pytest brackets the count as
    # "(21 deselected)" only under --collect-only, and prints "21 deselected"
    # for a run that executes. This case is the executing shape.
    deselected = re.search(r"(\d+) deselected", result.stdout)
    assert deselected and int(deselected.group(1)) >= 1, (
        f"{CHILD_TARGET} collected nothing, so this case no longer pins the "
        f"choice of hook.\n{result.stdout}"
    )


@pytest.mark.subsystem
def test_without_the_option_an_empty_selection_still_exits_five() -> None:
    """Assert the unguarded behaviour is unchanged -- the control for the case above

    Without this, the previous case would pass against any change that made the
    run exit 4 for some unrelated reason. Exit 5 is what pytest already did, and
    what every invocation that omits the option must keep doing.
    """
    result = _run_in_repository(
        "-q", "--collect-only", "-m", UNMATCHED_MARKER, str(CHILD_TARGET)
    )

    assert result.returncode == EXIT_NO_TESTS_COLLECTED, (
        f"Expected exit {EXIT_NO_TESTS_COLLECTED} (nothing collected), got "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.subsystem
def test_a_real_failure_keeps_its_own_exit_code(tmp_path: Path) -> None:
    """Assert a genuine test failure under a satisfied floor still exits 1

    This is the failure mode the design rejects the shell workaround for: a guard
    that converts exit 1 into anything else -- 0 especially -- turns a failing job
    green. ``1 failed, 1 passed`` is asserted as well as the exit code, because a
    plugin that failed to import would also exit 1 while running nothing at all.

    Args:
        tmp_path: pytest's per-test temporary directory, off the repository tree.
    """
    _write(
        tmp_path,
        "test_mixed.py",
        """
        def test_passes() -> None:
            pass


        def test_fails() -> None:
            assert False
        """,
    )

    result = _run_over_temporary_tests(
        "-q", f"{OPTION}=1", "test_mixed.py", cwd=tmp_path
    )

    assert result.returncode == EXIT_TESTS_FAILED, (
        f"Expected exit {EXIT_TESTS_FAILED} (tests failed), got "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )
    assert "1 failed, 1 passed" in result.stdout, (
        f"The run did not execute both tests; the guard or the plugin "
        f"interfered.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.subsystem
def test_a_satisfied_minimum_lets_a_passing_run_pass(tmp_path: Path) -> None:
    """Assert a satisfied floor leaves a passing run passing

    Executed rather than merely collected: ``--collect-only`` would show that
    collection was not aborted, which is a weaker claim than the run completing
    green with the guard in place.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _write(
        tmp_path,
        "test_passing.py",
        """
        def test_passes() -> None:
            pass
        """,
    )

    result = _run_over_temporary_tests(
        "-q", f"{OPTION}=1", "test_passing.py", cwd=tmp_path
    )

    assert result.returncode == EXIT_OK, (
        f"Expected exit {EXIT_OK}, got {result.returncode}.\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "1 passed" in result.stdout, (
        f"Expected one passing test.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.subsystem
def test_a_collection_error_is_not_rediagnosed_as_under_selection(
    tmp_path: Path,
) -> None:
    """Assert an import error keeps its own exit code and its own diagnosis

    A module that fails to import lowers the selected count, so an unconditional
    guard fires on top of it and the engineer's headline blames a marker
    expression that is innocent. Both halves are asserted: pytest's own exit 2
    survives, and the guard's message is *absent*.

    The arming condition reads ``session.testsfailed``, which is pytest's own
    counter rather than a documented contract. Measured non-zero at
    ``pytest_collection_finish`` on **pytest 9.1.1**, the version this was written
    against; §10.2 bounds the project at ``pytest>=9,<10``, so a minor release
    could in principle move when it is set. That is not a silent risk: were it to
    change, the guard would re-fire on the collection error and this case fails on
    its *first* assertion with ``Expected exit 2 …, got 4`` -- verified by
    simulating exactly that regression. The version is named here so the engineer
    who meets that failure after a pytest bump reads it as the deliberate decision
    it is, rather than as a puzzle.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _write(tmp_path, "test_fine.py", "def test_fine() -> None:\n    pass\n")
    _write(tmp_path, "test_broken.py", "import definitely_no_such_module\n")

    result = _run_over_temporary_tests("-q", f"{OPTION}=2", ".", cwd=tmp_path)

    assert result.returncode == EXIT_INTERRUPTED, (
        f"Expected exit {EXIT_INTERRUPTED} (collection interrupted), got "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )
    assert "definitely_no_such_module" in result.stdout, (
        f"The import error is not what was reported.\n{result.stdout}"
    )
    assert "expected at least 2" not in result.stderr, (
        "The guard fired on top of a collection error and blamed the -m "
        f"expression for it.\n{result.stderr}"
    )


@pytest.mark.subsystem
def test_the_option_is_unknown_outside_the_tests_tree() -> None:
    """Assert the option's documented reach -- it does not exist for other targets

    ``pytest_addoption`` is honoured in *initial* conftests only, and
    ``tests/conftest.py`` is one only for invocations resolving under ``tests/``.
    Every job section 10.3 defines selects by marker over ``tests/``, so the
    constraint binds nothing today; it is recorded and asserted because a job
    pointed anywhere else would fail with a message about a flag rather than
    about a selector, which is a confusing way to learn this.

    Asserted rather than left implicit so the constraint cannot rot: if a later
    change moves the registration to a repository-root ``conftest.py``, this case
    turns red and the design document gets updated with it.
    """
    result = _run_in_repository("-q", "--collect-only", f"{OPTION}=1", "src")

    assert result.returncode == EXIT_USAGE_ERROR, (
        f"Expected exit {EXIT_USAGE_ERROR}, got {result.returncode}.\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert UNRECOGNIZED in result.stderr, (
        f"Expected {OPTION} to be unknown for a target outside tests/.\n"
        f"{result.stderr}"
    )
