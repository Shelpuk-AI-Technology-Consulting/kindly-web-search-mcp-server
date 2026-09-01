"""Guard the baseline failure ledger against what a real run actually does.

``.system_design/BASELINE_FAILURES.md`` names, one node id at a time, every test
that fails on this repository today. This module holds that document and the
suite in agreement.

The document exists because ``TEST_SUITE.md`` §1.1 quotes the baseline as
*counts*, and a count is not a checkpoint: ``12 failed`` before a repair and
``11 failed`` after it is equally consistent with "one test was fixed" and with
"two were fixed and a third was broken". Only the *set* of failing node ids
separates those, which is what the ledger records and what this module asserts.

Three kinds of check live here, and none implies another:

* **structural**, which read only the document -- that every platform the guard
  knows has a section, that each block is sorted and free of duplicates, and
  that each block agrees with the still-failing count on its ``Remaining`` line.
  These run wherever the suite runs, so the section for the platform you are
  *not* on is still held to its format, and they are exercised against synthetic
  documents so that each rejection stays a test rather than a one-time
  observation;
* **cross-platform**, which holds the two blocks to the difference the document
  itself explains. Only one platform's live comparison can run on any given
  machine and there is no Windows lane in CI yet, so without this a repair that
  drains one block and forgets the other passes every check that executes;
* **behavioural**, which runs the suite in a child process and compares the live
  outcome to the documented one. This is the check that does the real work, and
  it runs only on a platform the ledger covers.

An id may leave the ledger **only by passing**. Deleting a test, renaming its
class, or marking it skipped also removes it from the failing set, and all three
look exactly like a repair. The child therefore reports its collected and
skipped sets as well, and this module names those outcomes separately -- see
:func:`test_live_outcome_matches_the_ledger`.

The ledger drains as E1-2 through E1-5 land, each deleting exactly the ids it
repairs. When both blocks are empty, the behavioural check is asserting that a
real run has no failures at all -- so the E1-6 milestone and this guard are the
same fact, checked once rather than twice. The guard outlives that milestone: a
drained ledger is then the cheapest available "the suite is green" assertion,
and deleting it would give the twelve repaired tests room to rot again.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_pytest_configuration import _section_body

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
LEDGER_PATH = REPO_ROOT / ".system_design" / "BASELINE_FAILURES.md"

# This module's path as pytest spells it, derived rather than written out. The
# child run must not collect this module -- one guard invocation has to spawn
# exactly one child, not a chain of them -- and deriving the path from
# ``__file__`` means renaming the module cannot silently disarm the exclusion and
# turn the guard into a fork bomb. ``as_posix`` because pytest takes forward
# slashes on both platforms.
GUARD_RELATIVE_PATH = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

# ``sys.platform`` to the ledger heading that covers it. The mapping is the
# guard's own table and is asserted against the document in both directions, so
# neither a section without an entry nor an entry without a section can pass
# unnoticed. A platform absent from this table -- macOS, say -- has no measured
# baseline, and the behavioural check skips there rather than inventing one.
LEDGER_PLATFORMS: dict[str, str] = {
    "linux": "## Linux",
    "win32": "## Windows",
}

DIFFERENCE_HEADING = "## Why the two platforms differ"
COMMAND_HEADING = "## The measurement command"

# Environment prefixes and names the child run does not inherit. A failing set is
# only reproducible if the run that produced it cannot be steered from outside,
# and this repository reads roughly fifty variables. Two are the live-test
# opt-ins, and the hazard is measured rather than theoretical: on Linux at commit
# 3af0563, exporting ``RUN_LIVE_TESTS=1`` alone adds a thirteenth failure
# (``test_serper_live.py::TestSerperLive::test_serper_search_live``, which reaches
# the network and fails on the dummy credential ``conftest.py`` installs).
# ``PYTEST_*`` covers an inherited ``PYTEST_ADDOPTS`` that would re-select the
# child's tests, and ``COVERAGE_*`` stops a parent running under ``coverage run``
# from enrolling the child in its own measurement.
#
# A hand-written list of a moving target rots, so
# :func:`test_child_environment_drops_every_variable_the_project_reads` recovers
# the variables the source tree actually reads and asserts that none survives
# this sweep. That is what keeps the sweep honest as variables are added.
CLEARED_ENVIRONMENT_PREFIXES = (
    "PYTHON",
    "KINDLY_",
    "SEARXNG_",
    "FASTMCP_",
    "MCP_",
    "STACKEXCHANGE_",
    "GITHUB_",
    "WIKIPEDIA_",
    "ARXIV_",
    "PYTEST_",
    "COVERAGE_",
)
CLEARED_ENVIRONMENT_NAMES = (
    "RUN_LIVE_TESTS",
    "SERPER_API_KEY",
    "SERPBASE_API_KEY",
    "SOFYA_API_KEY",
    "TAVILY_API_KEY",
    "YDC_API_KEY",
    "LOG_LEVEL",
    "NO_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
)

# An environment variable name, by shape: upper case with at least one
# underscore. Upper case **only**, deliberately. Widening the shape to lower case
# was measured before being rejected: it admits 139 more literals, of which 137
# have no upper-case counterpart anywhere in the tree and are ordinary dictionary
# keys and field names. That would trade a precise check for a 137-entry
# allow-list nobody maintains -- rot arriving faster than the rot it prevents.
# The lower-case reads this project does have (``no_proxy``) are covered from the
# other side instead: :func:`_child_environment` matches upper-cased, so one
# listed name covers both spellings, and the check below plants both. Every string literal in the tree of this shape is treated as a
# candidate, rather than only those appearing inside an ``os.environ.get("...")``
# call. Anchoring on the call form was measured wrong: this repository reads
# ``CHROME_BIN``, ``CHROME_PATH``, ``BROWSER_EXECUTABLE_PATH`` and ``no_proxy``
# through a *loop variable* over a tuple of literals, and reads others through
# ``_get_int_env(key, ...)`` helpers, so no call-site pattern can see them. A
# shape scan over-collects instead, which is the safe direction: a name that is
# not an environment variable costs one allow-list entry, while a name that is
# and was missed costs a silently steerable baseline.
ENVIRONMENT_NAME_SHAPE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

# Literals of that shape which are not environment variables. Each has to be
# named here deliberately, which is what makes the scan un-rottable: a new
# environment variable added to the tree is either swept or fails the check, and
# the only way to silence it is to claim in this list that it is not one.
NOT_ENVIRONMENT_VARIABLES = frozenset(
    {
        # Windows process-creation and ShowWindow constants.
        "CREATE_NEW_PROCESS_GROUP",
        "CREATE_NO_WINDOW",
        "STARTF_USESHOWWINDOW",
        "SW_HIDE",
        # A GitHub reaction name.
        "THUMBS_UP",
        # Fixture values in the diagnostics-masking and GitHub tests.
        "AUTH_BEARER",
        "SOME_URL",
        "C_1",
        "D_1",
        "R_1",
    }
)

# Generous next to the 120 seconds the other child-process guards allow, because
# this child runs the *whole* suite rather than a one-case throwaway module. On
# the measuring machine it takes about fourteen seconds; the margin is for a
# loaded CI runner. Only the direct child is killed on expiry -- the suite spawns
# grandchildren of its own -- which is accepted rather than solved: cross-platform
# process-group teardown is real machinery, and this path is reached only when
# something is already badly wrong.
CHILD_TIMEOUT_SECONDS = 300


def _ledger_text() -> str:
    """Read the ledger document.

    Returns:
        The whole of ``.system_design/BASELINE_FAILURES.md`` as text.

    Raises:
        AssertionError: When the document is missing. It is the subject of every
            check here, so its absence is a failure of this guard rather than an
            error to let propagate as a bare :class:`FileNotFoundError`.
    """
    assert LEDGER_PATH.is_file(), (
        f"{LEDGER_PATH} is missing. Every check in this module reads it; without "
        "it the suite's red baseline is recorded nowhere."
    )
    return LEDGER_PATH.read_text(encoding="utf-8")


def _documented_platform_headings(text: str) -> set[str]:
    """Return the headings of the document's platform sections.

    A platform section is identified by carrying a ``Result`` line -- the summary
    of the run it records -- rather than by matching its title against a pattern.
    That keeps the prose sections out without requiring them to avoid any
    particular wording, and it means a new platform section is recognised the
    moment someone adds one, which is what lets
    :func:`test_ledger_documents_every_platform_the_guard_knows` fail.

    Args:
        text: The whole document.

    Returns:
        Every level-2 heading whose section carries a ``Result`` line.
    """
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    return {
        heading
        for heading in headings
        if re.search(
            r"^\s*-\s+\*\*Result:\*\*", _section_body(text, heading), re.MULTILINE
        )
    }


def _fenced_block(text: str, heading: str, language: str) -> str:
    """Return the single fenced block of one language inside one section.

    The search is bounded to the section and requires exactly one block,
    following the shape set by :mod:`tests.test_pytest_configuration`: an
    unbounded search would start comparing against some later section's block the
    day one is added, and would do it silently.

    Args:
        text: The whole document.
        heading: The section's heading line.
        language: The fence's language tag, ``text`` or ``console``.

    Returns:
        The block's contents, fences excluded.

    Raises:
        AssertionError: When the heading is absent, or the section does not hold
            exactly one block of that language.
    """
    assert heading in text.splitlines(), (
        f"{LEDGER_PATH.name} no longer contains '{heading}'. This guard parses "
        "that section; renaming it would silently disable the check."
    )
    section = _section_body(text, heading)
    blocks = re.findall(rf"```{language}\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1, (
        f"Section '{heading}' of {LEDGER_PATH.name} contains {len(blocks)} "
        f"fenced ```{language} blocks; this guard requires exactly one."
    )
    return blocks[0]


def _ledger_node_ids(text: str, heading: str) -> list[str]:
    """Parse one platform section's node-id block.

    Args:
        text: The whole document.
        heading: The platform section's heading line.

    Returns:
        The node ids listed under ``heading``, in the order written, with blank
        lines dropped. An empty list is a legitimate answer: it is what the
        ledger says once every repair has landed.
    """
    block = _fenced_block(text, heading, "text")
    return [line.strip() for line in block.splitlines() if line.strip()]


def _recorded_failure_count(text: str, heading: str) -> int:
    """Parse the still-failing count from a platform section's ``Remaining`` line.

    This was originally read off the ``Result`` line, on the reasoning that a
    number taken from the recorded run cannot be edited to match the list while
    the run said something else. The reasoning was sound and the placement was
    not: a repair deletes ids, so the count has to change, and editing it in
    place turns a measurement into an arithmetic edit sitting between three
    figures that are still the original run's. The first repair to land would
    have produced ``7 failed, 303 passed, 2 skipped`` at a commit where no such
    run ever happened -- exactly what TEST_SUITE.md §1.1's "from real runs, or
    not quoted" rule exists to prevent.

    So the two facts are separate fields. ``Result`` is the measurement and is
    never edited again; ``Remaining`` is maintained alongside the block and is
    what this guard asserts. The redundancy lost by not reading it off the
    measurement is small -- :func:`test_live_outcome_matches_the_ledger`
    compares the block itself against a real run, which is a stronger check than
    any count -- and an immutable record of what was measured is worth more.

    Args:
        text: The whole document.
        heading: The platform section's heading line.

    Returns:
        The ``N`` in the section's ``**Remaining:** N failed`` line.

    Raises:
        AssertionError: When the section has no such line, or more than one.
    """
    section = _section_body(text, heading)
    counts = re.findall(
        r"^\s*-\s+\*\*Remaining:\*\*\s+(\d+) failed\b", section, re.MULTILINE
    )
    assert len(counts) == 1, (
        f"Section '{heading}' of {LEDGER_PATH.name} has {len(counts)} lines of "
        "the form '- **Remaining:** N failed'; this guard requires exactly one."
    )
    return int(counts[0])


def _documented_platform_only_ids(text: str, platform: str) -> list[str]:
    """Parse the ids the document says fail on one platform and not the other.

    Args:
        text: The whole document.
        platform: The ``sys.platform`` key whose exclusive ids are wanted.

    Returns:
        The node ids listed for ``platform`` in the difference section, sorted.
    """
    block = _fenced_block(text, DIFFERENCE_HEADING, "text")
    prefix = f"{platform}:"
    return sorted(
        line.strip().removeprefix(prefix).strip()
        for line in block.splitlines()
        if line.strip().startswith(prefix)
    )


def _child_command(probe_path: Path) -> list[str]:
    """Build the child run's argv.

    Args:
        probe_path: Where the probe plugin writes its JSON result.

    Returns:
        The full command line, ``sys.executable`` first.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests._baseline_probe",
        "-c",
        str(PYPROJECT_PATH),
        f"--ignore={GUARD_RELATIVE_PATH}",
        f"--baseline-probe-json={probe_path}",
        "-q",
        "--tb=no",
    ]


def _normalised_child_command(probe_path: Path) -> str:
    """Render the child's argv in the form the ledger documents.

    Three tokens vary by machine and would make a literal comparison against the
    document meaningless: the interpreter, the absolute path to
    ``pyproject.toml`` and the temporary file the probe writes. Each is replaced
    by a stable placeholder, so what remains -- the selection, the plugins, the
    exclusion -- is exactly the part that defines which run the baseline
    describes.

    Args:
        probe_path: The path used in this invocation, replaced by a placeholder.

    Returns:
        One line, suitable for comparing against the document's block.
    """
    rendered = " ".join(_child_command(probe_path))
    rendered = rendered.replace(sys.executable, "python", 1)
    rendered = rendered.replace(str(PYPROJECT_PATH), "<repo>/pyproject.toml", 1)
    return rendered.replace(str(probe_path), "<tempfile>", 1)


def _child_environment() -> dict[str, str]:
    """Build the child's environment.

    A cleaned copy, not a cleared one: the child still needs ``PATH``, ``HOME``
    and the rest of the ambient environment to start an interpreter at all. What
    it must not inherit is anything that steers this project -- see
    :data:`CLEARED_ENVIRONMENT_PREFIXES`.

    Returns:
        The environment the child is spawned with.
    """
    # Matched upper-cased: ``nodriver_worker`` reads both ``NO_PROXY`` and
    # ``no_proxy``, and a case-sensitive sweep would drop one and keep the other.
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(CLEARED_ENVIRONMENT_PREFIXES)
        and name.upper() not in CLEARED_ENVIRONMENT_NAMES
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _environment_variables_the_project_reads() -> set[str]:
    """Recover the environment variable names the source tree could read.

    Parsed rather than grepped, so a name inside a comment cannot register while
    one built across a line continuation goes missing.

    Returns:
        Every string literal under ``src/`` or ``tests/`` shaped like an
        environment variable name, less those declared in
        :data:`NOT_ENVIRONMENT_VARIABLES`.
    """
    sources = (*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "tests").rglob("*.py"))
    names: set[str] = set()
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ENVIRONMENT_NAME_SHAPE.match(node.value)
            ):
                names.add(node.value)
    return names - NOT_ENVIRONMENT_VARIABLES


def _run_suite_in_child(probe_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the whole suite in a child process, excluding this module.

    The child is pointed at the committed ``pyproject.toml`` with ``-c`` and run
    from the repository root, so it collects the same tests an ordinary
    ``pytest`` invocation does. ``encoding`` is stated rather than left to the
    locale, which on Windows is cp1252 and has undefined bytes; the child is told
    to write UTF-8, so the parent must be told to read it.

    Args:
        probe_path: Where the probe plugin writes its JSON result.

    Returns:
        The completed child, with ``stdout`` and ``stderr`` captured as text.

    Raises:
        Failed: When the child has not exited within
            :data:`CHILD_TIMEOUT_SECONDS`. Reported through :func:`pytest.fail`
            rather than as a bare :class:`subprocess.TimeoutExpired` traceback,
            so the one path here that can hang still explains itself.
    """
    try:
        return subprocess.run(
            _child_command(probe_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
            env=_child_environment(),
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        pytest.fail(
            f"The child pytest run did not finish within {CHILD_TIMEOUT_SECONDS}s; "
            "it normally takes about fourteen. Partial output:\n"
            f"{expired.stdout or ''}\n{expired.stderr or ''}"
        )


def _probe_results(probe_path: Path) -> dict[str, set[str]]:
    """Read the probe plugin's JSON output.

    Args:
        probe_path: The file the child was told to write.

    Returns:
        The ``collected``, ``failed`` and ``skipped`` node-id sets.

    Raises:
        AssertionError: When the child produced no file, which means the plugin
            never loaded and every comparison below would be vacuous.
    """
    assert probe_path.is_file(), (
        f"The child run wrote no probe output to {probe_path}. The "
        "'tests._baseline_probe' plugin did not load, so this guard has nothing "
        "to compare and must not report success."
    )
    recorded = json.loads(probe_path.read_text(encoding="utf-8"))
    return {key: set(value) for key, value in recorded.items()}


def _synthetic_ledger(
    node_ids: list[str], *, recorded: int | None = None, extra_block: bool = False
) -> str:
    """Build a minimal document for exercising the parsers.

    The structural checks are properties of the parsers, and a property nothing
    executes is an observation someone once made. Feeding the parsers documents
    built here keeps each rejection a test.

    Args:
        node_ids: The ids to place in the block.
        recorded: The still-failing count to state on the ``Remaining`` line;
            defaults to ``len(node_ids)``.
        extra_block: When true, add a second fenced block to the section.

    Returns:
        A document with a single ``## Linux`` platform section, carrying both
        the frozen ``Result`` measurement and the maintained ``Remaining``
        count, because the shipped parsers read one field each. The two are
        deliberately different numbers here, so a parser reading the wrong one
        is caught by these cases rather than by the real document.
    """
    stated = len(node_ids) if recorded is None else recorded
    listing = "\n".join(node_ids)
    second = "\n```text\nstray\n```\n" if extra_block else ""
    return (
        "# Synthetic\n\n## Linux\n\n"
        "- **Result:** 99 failed, 1 passed\n"
        f"- **Remaining:** {stated} failed\n\n"
        f"```text\n{listing}\n```\n{second}"
    )


def _assert_block_is_sorted_and_unique(text: str, heading: str) -> None:
    """Assert one platform's block is a set written in one canonical order.

    Sorted order is required so that a repair step's diff shows exactly the lines
    it removed. An unsorted list reorders under editing and buries a one-line
    change in a whole-block rewrite.

    Shared by the check that runs against the real document and the one that runs
    against synthetic documents, so the synthetic cases exercise the shipped
    assertion rather than a restatement of it that could drift from it.

    Args:
        text: The whole document.
        heading: The platform section's heading line.

    Raises:
        AssertionError: When the block is unsorted or holds a duplicate.
    """
    node_ids = _ledger_node_ids(text, heading)
    assert node_ids == sorted(set(node_ids)), (
        f"Section '{heading}' of {LEDGER_PATH.name} must list its node ids "
        "sorted and without duplicates. Expected:\n" + "\n".join(sorted(set(node_ids)))
    )


def _assert_count_matches_node_ids(text: str, heading: str) -> None:
    """Assert one platform's stated failure count equals the length of its list.

    Both are maintained by hand, so they drift the moment someone deletes an id
    without touching the count -- which is exactly what the repair steps will be
    doing. The pairing is what makes a half-finished drain visible.

    Args:
        text: The whole document.
        heading: The platform section's heading line.

    Raises:
        AssertionError: When the ``Remaining`` line and the block disagree.
    """
    node_ids = _ledger_node_ids(text, heading)
    recorded = _recorded_failure_count(text, heading)
    assert recorded == len(node_ids), (
        f"Section '{heading}' of {LEDGER_PATH.name} records {recorded} failures "
        f"but lists {len(node_ids)} node ids."
    )


def test_ledger_documents_every_platform_the_guard_knows() -> None:
    """Assert the document's platform sections match this module's table.

    Checked in both directions. A section removed from the document would
    otherwise leave the platform it covered silently unguarded, and a section
    added without a table entry would never be compared against anything.
    """
    documented = _documented_platform_headings(_ledger_text())
    expected = set(LEDGER_PLATFORMS.values())
    assert documented == expected, (
        f"{LEDGER_PATH.name} documents platform sections {sorted(documented)} "
        f"but this guard knows {sorted(expected)}. Sections and table entries "
        "are added together, or one of them guards nothing."
    )


@pytest.mark.parametrize("heading", sorted(LEDGER_PLATFORMS.values()))
def test_ledger_block_is_sorted_and_free_of_duplicates(heading: str) -> None:
    """Assert one platform's block is sorted and holds no duplicate.

    Args:
        heading: The platform section to check, injected by parametrisation.
    """
    _assert_block_is_sorted_and_unique(_ledger_text(), heading)


@pytest.mark.parametrize("heading", sorted(LEDGER_PLATFORMS.values()))
def test_ledger_count_matches_its_node_ids(heading: str) -> None:
    """Assert one platform's stated failure count matches its list.

    Args:
        heading: The platform section to check, injected by parametrisation.
    """
    _assert_count_matches_node_ids(_ledger_text(), heading)


def test_documented_platform_difference_explains_the_two_blocks() -> None:
    """Assert the two platforms' blocks differ by exactly what the document says.

    The blocks overlap by construction -- the same stale tests fail on both -- so
    every repair must delete its ids from both. Only one block's live comparison
    can run on any one machine, and there is no Windows lane in CI, so a repair
    that drains Linux and forgets Windows would otherwise pass every check that
    actually executes. This one runs everywhere.
    """
    text = _ledger_text()
    linux = set(_ledger_node_ids(text, LEDGER_PLATFORMS["linux"]))
    windows = set(_ledger_node_ids(text, LEDGER_PLATFORMS["win32"]))
    for platform, exclusive in (("linux", linux - windows), ("win32", windows - linux)):
        documented = _documented_platform_only_ids(text, platform)
        assert sorted(exclusive) == documented, (
            f"The ledger's blocks differ on {platform} by {sorted(exclusive)}, "
            f"but '{DIFFERENCE_HEADING}' explains {documented}. Every difference "
            "between the two platforms needs a stated reason, or a half-drained "
            "repair reads as a platform quirk."
        )


def test_ledger_documents_the_measurement_command(tmp_path: Path) -> None:
    """Assert the document records the exact run the baseline describes.

    A failing set means nothing without the selection that produced it. Today an
    unfiltered run is the whole suite, but later workstreams apply the ``live``,
    ``chromium`` and ``package`` markers, at which point a different selection
    yields a different baseline. Recording the argv makes any such change a
    visible, deliberate edit to this document rather than a silent redefinition.

    Args:
        tmp_path: pytest's per-test temporary directory, used only to supply a
            placeholder path to the renderer.
    """
    documented = _fenced_block(_ledger_text(), COMMAND_HEADING, "console").strip()
    actual = _normalised_child_command(tmp_path / "probe.json")
    assert documented == actual, (
        f"Section '{COMMAND_HEADING}' of {LEDGER_PATH.name} records\n"
        f"  {documented}\nbut this guard runs\n  {actual}"
    )


def test_child_environment_drops_every_variable_the_project_reads() -> None:
    """Assert the sweep covers every environment variable the source tree reads.

    The sweep is a deny-list over a surface of roughly fifty variables, and a
    hand-written deny-list of a moving target rots. Rather than trust it, the
    names are recovered from the source and each is required to be absent from
    the environment the child would be given. Each is planted first, so the check
    fails on a clean machine too rather than only on one that happens to export
    the variable in question.
    """
    names = _environment_variables_the_project_reads()
    assert names, (
        "No environment reads were found under src/ or tests/. The scan pattern "
        "has stopped matching, so this check would pass vacuously."
    )
    original = os.environ.copy()
    # Both cases, since the reads are not all upper case and the sweep claims to
    # cover either.
    os.environ.update({name: "planted" for name in names})
    os.environ.update({name.lower(): "planted" for name in names})
    # Planted explicitly rather than relied upon: the scan's shape is upper case
    # only, so `no_proxy` never arrives through `names`, and reading it from the
    # ambient environment would make the control fire only on a machine that
    # happens to export it -- passing vacuously everywhere else, which is the
    # failure mode this whole check exists to prevent.
    os.environ["no_proxy"] = "planted"
    try:
        child = _child_environment()
        surviving = {name.upper() for name in child}
        survivors = sorted(names & surviving)
        lowercase_survived = "no_proxy" in child
    finally:
        os.environ.clear()
        os.environ.update(original)
    # The control for the case that motivated matching case-insensitively: the
    # one lower-case variable this project reads gets a check of its own rather
    # than incidental coverage.
    assert not lowercase_survived, (
        "`no_proxy` survives the sweep. It is read at scrape/nodriver_worker.py "
        "and scrape/universal_html.py alongside `NO_PROXY`, and the sweep is "
        "case-insensitive precisely so that listing the upper-case name covers "
        "both spellings."
    )
    assert not survivors, (
        "The child run would inherit project environment variables "
        f"{survivors}, so its failing set is steerable from outside. Add each to "
        "CLEARED_ENVIRONMENT_PREFIXES or CLEARED_ENVIRONMENT_NAMES."
    )


@pytest.mark.parametrize(
    ("text", "check", "expectation"),
    [
        pytest.param(
            _synthetic_ledger(["b::t", "a::t"]),
            _assert_block_is_sorted_and_unique,
            "sorted",
            id="unsorted",
        ),
        pytest.param(
            _synthetic_ledger(["a::t", "a::t"]),
            _assert_block_is_sorted_and_unique,
            "sorted",
            id="duplicated",
        ),
        pytest.param(
            _synthetic_ledger(["a::t"], recorded=9),
            _assert_count_matches_node_ids,
            "records 9",
            id="count-mismatch",
        ),
        pytest.param(
            _synthetic_ledger(["a::t"], extra_block=True),
            _assert_block_is_sorted_and_unique,
            "requires exactly one",
            id="two-blocks",
        ),
        pytest.param(
            "# Synthetic\n\n## Windows\n\n- **Result:** 0 failed\n"
            "- **Remaining:** 0 failed\n\n```text\n```\n",
            _assert_block_is_sorted_and_unique,
            "no longer contains",
            id="missing-heading",
        ),
    ],
)
def test_parsers_reject_a_malformed_section(
    text: str, check: Callable[[str, str], None], expectation: str
) -> None:
    """Assert each malformed document shape is rejected, with a legible message.

    These are the shapes a repair step will actually produce -- a deleted id
    without a count change, a hand-reordered block -- so they are exercised
    against synthetic documents rather than confirmed once by hand against the
    real one.

    The assertion under test is a parameter rather than a call chain: with both
    helpers invoked in sequence, only the count case ever reached the second, and
    nothing said so.

    Args:
        text: The synthetic document.
        check: The assertion this shape is expected to trip.
        expectation: A phrase the rejection message must contain.
    """
    with pytest.raises(AssertionError, match=expectation):
        check(text, "## Linux")


def test_parsers_accept_a_drained_section() -> None:
    """Assert an empty block with a zero count is valid.

    The drained state occurs exactly once, at the milestone, when it matters
    most. A parser that rejected it would turn the moment the suite goes green
    into a debugging session.
    """
    text = _synthetic_ledger([])
    _assert_block_is_sorted_and_unique(text, "## Linux")
    _assert_count_matches_node_ids(text, "## Linux")
    assert _ledger_node_ids(text, "## Linux") == []


@pytest.mark.subsystem
@pytest.mark.slow
def test_live_outcome_matches_the_ledger(tmp_path: Path) -> None:
    """Assert a real run fails at exactly the node ids the ledger names.

    The check the whole file exists for. It runs the suite in a child process and
    compares, so it is measuring behaviour rather than re-reading the document.

    Four outcomes are reported separately because they call for opposite fixes,
    and a single set-inequality message would be the wrong thing to read at three
    in the morning: a failure the ledger does not list is a regression; a listed
    id that no longer exists is a deleted or renamed test, not a repair; a listed
    id that now skips is a test that stopped running; and only a listed id that
    ran and passed is a repair whose entry should be deleted.

    Args:
        tmp_path: pytest's per-test temporary directory, where the child's probe
            output is written.
    """
    # Nothing to compare against on a platform nobody measured. Skipping loudly
    # beats passing silently: a guard that cannot fail is indistinguishable from
    # no guard.
    if sys.platform not in LEDGER_PLATFORMS:
        pytest.skip(
            f"{LEDGER_PATH.name} records no baseline for sys.platform="
            f"{sys.platform!r}; it covers {sorted(LEDGER_PLATFORMS)}."
        )

    probe_path = tmp_path / "probe.json"
    completed = _run_suite_in_child(probe_path)
    # Exit 0 means the ledger should be empty and exit 1 means it should not;
    # both are compared below. Any other code -- 2 for a collection error, 3 for
    # an internal error, 4 for a usage error -- means the child never produced a
    # comparable result, so it is reported as itself rather than as a mismatch.
    assert completed.returncode in (0, 1), (
        f"The child pytest run exited {completed.returncode}, so it produced no "
        "comparable outcome.\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )

    outcome = _probe_results(probe_path)
    # Exit 1 means pytest counted at least one failure, so a recording that found
    # none has gone blind -- which is exactly how an earlier, summary-parsing
    # version of this guard missed subtest failures entirely. Cheap, and it fails
    # loudly rather than reporting an empty set as good news.
    assert (completed.returncode == 1) == bool(outcome["failed"]), (
        f"The child exited {completed.returncode} but the probe recorded "
        f"{len(outcome['failed'])} failing node ids. The two disagree, so the "
        "recording path is broken and its result must not be trusted.\n"
        f"stdout:\n{completed.stdout}"
    )

    documented = set(_ledger_node_ids(_ledger_text(), LEDGER_PLATFORMS[sys.platform]))
    complaints = {
        "Failing but not in the ledger (a regression; fix the test, do not add "
        "the id)": sorted(outcome["failed"] - documented),
        "In the ledger but no longer collected (deleted or renamed, not "
        "repaired; an id may leave this ledger only by passing)": sorted(
            documented - outcome["collected"]
        ),
        "In the ledger and now skipped (it stopped running; that is not a "
        "repair either)": sorted(documented & outcome["skipped"] - outcome["failed"]),
        "In the ledger, ran, and passed (a repair; delete the id)": sorted(
            documented & outcome["collected"] - outcome["failed"] - outcome["skipped"]
        ),
    }
    reported = {title: ids for title, ids in complaints.items() if ids}
    assert not reported, (
        f"The live run disagrees with {LEDGER_PATH.name} on {sys.platform}.\n"
        + "\n".join(
            f"{title}:\n  " + "\n  ".join(ids) for title, ids in reported.items()
        )
    )
