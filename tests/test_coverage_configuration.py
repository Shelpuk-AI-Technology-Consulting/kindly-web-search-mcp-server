"""Guard the three coverage configurations declared in ``.coveragerc*``.

Section 10.4 of ``.system_design/TEST_SUITE.md`` is the policy; ``.coveragerc``,
``.coveragerc-gate`` and ``.coveragerc-subprocess`` are what coverage.py actually
reads. This module keeps the two in step, in the same guard shape as
:mod:`tests.test_pytest_configuration` and :mod:`tests.test_min_selected_guard`.

Each configuration is checked **two ways, and both are needed** -- the same
two-kinds framing :mod:`tests.test_pytest_configuration` uses for the pytest
table:

* **By the values coverage.py resolved**, read through coverage.py's own parser
  and compared as the configuration's *whole difference* from coverage's
  defaults. An unrecognised key in a coveragerc -- ``source_pkg`` for
  ``source_pkgs``, a misspelled ``patch`` -- produces a
  :class:`~coverage.exceptions.CoverageWarning` and is *otherwise ignored*, so a
  text comparison finds the typo identical in document and file and passes while
  the coverage run measures the wrong thing. Only the resolved value can fail on
  that. Comparing the whole difference, rather than a list of keys someone
  remembered, also means an option added to a shipped file cannot take effect
  unnoticed.
* **By the settings the file actually spells out**, read with
  :mod:`configparser` -- keys *and* values. Resolved values alone are not
  enough, because coverage.py *derives* some of them: ``post_process`` sets
  ``parallel`` whenever ``patch`` contains ``subprocess``. So ``parallel = true``
  could be deleted from ``.coveragerc-subprocess``, or set to ``false``, and
  every resolved-value comparison would still pass -- the second leaving the
  file stating the opposite of the design it implements. That file says in its
  own comment that the setting is declared rather than inherited so the fact is
  legible; nothing but this check can hold it.

Neither kind implies the other. A declared key can be a typo coverage ignores; a
resolved value can be one coverage supplied by itself.

The behavioural cases at the end run real coverage in a child process, because
section 10.4's central claim -- that a module no test imports is reported at zero
rather than being absent -- is a claim about what coverage.py *does*, and no
amount of reading the configuration file can establish it.

Every measurement quoted in this module was taken on **both 7.13.5 and 7.16.0**
and agreed; individual claims below do not repeat the versions.
Those are the two ends that matter: section 10.2 bounds the project at
``coverage>=7.10.3,<8``, the pinned lane that runs the controls installs
``7.13.5`` exactly, and a developer's ``dev`` environment resolves to whatever is
current. The versions are named so that an engineer meeting a failure after a
coverage bump reads it as new behaviour rather than as a puzzle.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import coverage
import pytest
from coverage.exceptions import CoverageWarning

from tests.test_pytest_configuration import _section_body

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SUITE_PATH = REPO_ROOT / ".system_design" / "TEST_SUITE.md"

DESIGN_SECTION_HEADING = "### 10.4 Coverage"

PACKAGE = "kindly_web_search_mcp_server"

BASE_CONFIG = ".coveragerc"
GATE_CONFIG = ".coveragerc-gate"
SUBPROCESS_CONFIG = ".coveragerc-subprocess"

# The ``[run]`` settings every one of the three configurations shares, expressed
# as the attribute names coverage.py resolves them to rather than as the keys the
# file spells. ``source_pkgs`` is the whole point of control 1: it is what makes
# coverage.py report a module no test ever imported, instead of leaving it out of
# the report and out of everyone's attention.
COMMON_RUN_SETTINGS: dict[str, object] = {
    "source_pkgs": [PACKAGE],
    "branch": True,
    "relative_files": True,
}

# Section 10.4's ``[paths]`` mapping. Present in the two gating configurations and
# deliberately absent from the subprocess one; that absence has a reason worth
# reading and it is kept in one place, the trailing comment in
# ``.coveragerc-subprocess``, rather than restated here where it would drift.
EXPECTED_PATHS: dict[str, list[str]] = {
    "source": [f"src/{PACKAGE}", f"*/site-packages/{PACKAGE}"]
}

# The modules section 10.4 classifies as having no hermetic seam, in the order
# the document lists them. ``worker_runner.py`` does not exist yet -- it is the
# process-management module a later step extracts from ``universal_html.py`` --
# and coverage.py tolerates an ``omit`` pattern that matches nothing, so the list
# ships as designed.
EXPECTED_OMIT: list[str] = [
    "*/scrape/nodriver_worker.py",
    "*/scrape/chromium_pool.py",
    "*/scrape/worker_runner.py",
]

# What each shipped file must change relative to coverage.py's own defaults --
# every setting, not a chosen subset. Compared as a whole mapping, so a file that
# grows an extra option fails here rather than taking effect in silence.
EXPECTED_CONFIGURATIONS: dict[str, dict[str, object]] = {
    BASE_CONFIG: {**COMMON_RUN_SETTINGS, "paths": EXPECTED_PATHS},
    GATE_CONFIG: {
        **COMMON_RUN_SETTINGS,
        "run_omit": EXPECTED_OMIT,
        "paths": EXPECTED_PATHS,
    },
    SUBPROCESS_CONFIG: {
        **COMMON_RUN_SETTINGS,
        "parallel": True,
        "patch": ["subprocess"],
    },
}

# Attributes of :class:`~coverage.config.CoverageConfig` that record *which file*
# was read rather than what it said. They differ between any two configurations
# by construction, so comparing them would compare file names, not settings.
PROVENANCE_ATTRIBUTES = frozenset(
    {"config_file", "config_files_attempted", "config_files_read"}
)

# Attributes that are not settings a coveragerc declares: the file's own raw
# bytes, and the two that only a ``Coverage(...)`` constructor argument can set.
# Named explicitly rather than filtered by leading underscore, which is the
# obvious shape and the wrong one: ``CONFIG_FILE_OPTIONS`` contains
# ``("_crash", "run:_crash")``, a genuine file-settable option that makes
# coverage raise on a matching call stack. An underscore filter hides it, and a
# ``_crash`` added to a shipped file would pass both kinds of check here while
# breaking the coverage job outright. Excluding ``_config_contents`` is
# load-bearing rather than cosmetic: the shipped files carry comments the design
# document's blocks do not, so the document comparison cannot pass with it in.
DERIVED_ATTRIBUTES = frozenset({"_config_contents", "_include", "_omit"})

# What each file must *declare*, section by section and value by value, as
# configparser reads it. The counterpart to the resolved-value table above, and
# the only check that can see a setting coverage would have derived anyway.
#
# Values, not just key names. Comparing key sets catches the lesser sin -- a line
# deleted -- and misses the greater one: ``parallel = false`` states the opposite
# of the design, and because ``post_process`` forces it back to ``True`` the
# resolved value never moves and the key is still there. Measured: with key sets
# alone, all nineteen cases passed against that file.
#
# Each value is held as its non-empty lines, stripped, so a multi-value option
# reads as the list it is. Indentation is configparser's business and carries no
# meaning; ``true`` against ``false`` still differs, which is the point.
EXPECTED_DECLARED_SETTINGS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    BASE_CONFIG: {
        "run": {
            "source_pkgs": (PACKAGE,),
            "branch": ("true",),
            "relative_files": ("true",),
        },
        "paths": {"source": tuple(EXPECTED_PATHS["source"])},
    },
    GATE_CONFIG: {
        "run": {
            "source_pkgs": (PACKAGE,),
            "branch": ("true",),
            "relative_files": ("true",),
            "omit": tuple(EXPECTED_OMIT),
        },
        "paths": {"source": tuple(EXPECTED_PATHS["source"])},
    },
    SUBPROCESS_CONFIG: {
        "run": {
            "source_pkgs": (PACKAGE,),
            "branch": ("true",),
            "relative_files": ("true",),
            "parallel": ("true",),
            "patch": ("subprocess",),
        },
    },
}

# Environment variables coverage.py applies *after* the configuration file, and
# which therefore mask a file setting of the same name. Measured: with
# ``COVERAGE_FILE`` exported, a shipped ``data_file =`` vanishes from the
# comparison entirely, because both sides of the diff get the same override. That
# is not academic -- section 10.4 requires every job to set ``COVERAGE_FILE`` to
# an absolute path, so without this scrubbing the guard would be weaker in CI
# than on the machine it was written on.
MASKING_VARIABLES = (
    "COVERAGE_FILE",
    "COVERAGE_CORE",
    "COVERAGE_DEBUG",
    "COVERAGE_FORCE_CONFIG",
)

# Variables the coverage ``.pth`` hook reads to auto-start measurement in any
# child process. A CI job running this suite under ``.coveragerc-subprocess``
# exports them, and an inherited value would silently start a second, differently
# configured coverage inside every child these cases spawn.
INHERITED_COVERAGE_STARTUP_VARIABLES = (
    "COVERAGE_PROCESS_CONFIG",
    "COVERAGE_PROCESS_START",
)

# The module the probe imports. Chosen for two properties, both of which keep a
# failure here meaning what it says: it imports nothing outside the standard
# library, so a broken runtime dependency cannot turn this guard red for a reason
# that has nothing to do with coverage configuration; and importing it only
# defines a function, so the probe has no side effects. It is also outside
# ``scrape/``, where the omit patterns live.
PROBE_MODULE = f"{PACKAGE}.utils.logging"
PROBE_REPORT_PATH = f"src/{PACKAGE}/utils/logging.py"

# The module with no test file anywhere -- 213 statements, and the concrete gap
# that made control 1 necessary. It is the observable for both the whole-package
# claim and the exemption claim.
UNEXECUTED_REPORT_PATH = f"src/{PACKAGE}/scrape/chromium_pool.py"

# The opening of coverage.py's warning for an option it does not know. Spelled
# once so the check and its control cannot drift onto different substrings, at
# which point the control would stop controlling anything.
UNRECOGNISED_OPTION = "Unrecognized option"
EXEMPT_REPORT_PATH = f"src/{PACKAGE}/scrape/nodriver_worker.py"


def _config_path(name: str) -> Path:
    """Return a shipped configuration file's path, asserting it is there.

    Checked here rather than left to coverage.py, which answers a missing file
    with a :class:`~coverage.exceptions.ConfigError` traceback. Every case in
    this module is meaningless without the file, so all of them reach it through
    this helper and fail with one sentence naming the document that requires it.

    It returns an **absolute** path, and that is not merely tidiness.
    ``config_files_to_try`` special-cases the literal string ``".coveragerc"`` to
    mean "no file specified", and the unspecified branch then reads
    ``COVERAGE_RCFILE`` from the environment. Every job section 10.4 defines
    exports that variable, so a guard passing the bare relative name would
    quietly assert against whatever the job pointed at -- three times over, and
    green.

    Args:
        name: The configuration file's name at the repository root.

    Returns:
        The absolute path to the file.

    Raises:
        AssertionError: When the file does not exist.
    """
    path = REPO_ROOT / name
    assert path.is_file(), (
        f"{name} does not exist. Section 10.4 of {TEST_SUITE_PATH.name} "
        "requires it at the repository root."
    )
    return path


def _resolved_settings(config_path: Path) -> dict[str, object]:
    """Load a coveragerc through coverage.py and return what it changes.

    The configuration is diffed against one coverage.py built from an effectively
    empty file in the same process, so the result names exactly the settings this
    file establishes. Doing it as a diff rather than as a fixed list of
    attributes is what lets an unexpected option fail the comparison.

    Args:
        config_path: The configuration file to load.

    Two limits are worth knowing. A setting whose value happens to equal
    coverage's default is invisible by construction -- that is the price of
    expressing this as a difference at all. And a setting coverage *derives*
    rather than reads looks identical to one the file declared, which is why
    :func:`_declared_keys` exists alongside this.

    The baseline is read from :data:`os.devnull`, which is accepted rather than
    raising ``Couldn't read ... as a config file``: ``from_file`` counts a
    specified file as read whatever it contains.

    Returns:
        Every non-default setting, keyed by coverage.py's own attribute name.
    """
    # Both objects are built with the masking variables removed, so a shipped
    # setting cannot hide behind an override that lands on either side equally.
    # Restored by hand rather than with ``unittest.mock.patch.dict``: importing
    # anything from ``unittest`` makes the plan validator read this pytest-native
    # module as unittest-style and demand a migration batch for it.
    saved = {name: os.environ.pop(name, None) for name in MASKING_VARIABLES}
    try:
        # The baseline is read from a file rather than from ``config_file=False``
        # so that both sides go down the identical code path.
        defaults = coverage.Coverage(config_file=os.devnull).config
        actual = coverage.Coverage(config_file=str(config_path)).config
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})
    return {
        name: value
        for name, value in vars(actual).items()
        if name not in DERIVED_ATTRIBUTES
        and name not in PROVENANCE_ATTRIBUTES
        and value != getattr(defaults, name, None)
    }


def _declared_settings(config_path: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Return the sections, keys and values a coveragerc literally declares.

    Read with :mod:`configparser` rather than through coverage.py, which is the
    one job coverage.py cannot do here: it reports the configuration *after*
    ``post_process``, where ``parallel`` has been set from ``patch`` and a
    declared setting is indistinguishable from a derived one.

    ``interpolation=None`` matches how coverage reads its own files. With
    configparser's default ``BasicInterpolation`` a ``%`` in an omit pattern
    would raise the moment a value is read -- which is a difference this guard
    would introduce, not one the shipped configuration has.

    One deliberate strictness: coverage also accepts these settings under a
    ``[coverage:run]`` heading, which this function would report as a section
    literally named ``coverage:run`` and so fail. That is fail-closed and
    intended -- it pins the canonical section shape section 10.4 displays.

    Args:
        config_path: The configuration file to read.

    Returns:
        Each option's value as its non-empty stripped lines, keyed by section
        and option name.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    return {
        section: {
            option: tuple(
                line.strip()
                for line in parser.get(section, option, raw=True).splitlines()
                if line.strip()
            )
            for option in parser.options(section)
        }
        for section in parser.sections()
    }


@pytest.mark.parametrize("name", sorted(EXPECTED_DECLARED_SETTINGS))
def test_configuration_file_spells_out_exactly_what_the_design_shows(
    name: str,
) -> None:
    """Assert one shipped coveragerc declares section 10.4's settings verbatim

    The companion to the resolved-value case below, and not redundant with it.
    ``parallel`` is the concrete reason: coverage derives it from
    ``patch = subprocess``, so both deleting the line from
    ``.coveragerc-subprocess`` and setting it to ``false`` leave every resolved
    value untouched. The first loses only the file's stated intent that the
    setting be legible rather than inferred; the second leaves the file saying
    the opposite of the design it is supposed to implement. Nothing but this
    case can fail on either.

    It also catches the mirror image: a setting changed in a file and in section
    10.4 together, which the document comparison cannot see because both sides
    move.

    Args:
        name: The configuration file's name at the repository root.
    """
    declared = _declared_settings(_config_path(name))

    assert declared == EXPECTED_DECLARED_SETTINGS[name], (
        f"{name} declares {declared!r}, but section 10.4 of "
        f"{TEST_SUITE_PATH.name} shows {EXPECTED_DECLARED_SETTINGS[name]!r}."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_CONFIGURATIONS))
def test_configuration_file_declares_exactly_what_the_design_requires(
    name: str,
) -> None:
    """Assert one shipped coveragerc resolves to section 10.4's settings, and no others

    The comparison is of resolved values, so the file may carry as much comment
    as it needs; it is a whole-mapping comparison, so an added option fails it.

    Args:
        name: The configuration file's name at the repository root.
    """
    resolved = _resolved_settings(_config_path(name))

    assert resolved == EXPECTED_CONFIGURATIONS[name], (
        f"{name} resolves to {resolved!r}, but "
        f"section 10.4 of {TEST_SUITE_PATH.name} requires "
        f"{EXPECTED_CONFIGURATIONS[name]!r}."
    )


def test_the_gate_configuration_is_the_base_plus_only_an_omit_list() -> None:
    """Assert the gating config differs from the base in the omit list alone

    Section 10.4 describes it in exactly those words -- "the same file plus an
    ``omit`` list" -- and the two controls it feeds are only comparable with
    control 1's view if nothing else differs. Asserted as a relationship rather
    than left implicit in two separate expected tables, so the two cannot drift
    apart in some third field while both still match their own row.
    """
    base = _resolved_settings(_config_path(BASE_CONFIG))
    gate = _resolved_settings(_config_path(GATE_CONFIG))

    assert gate.pop("run_omit", None) == EXPECTED_OMIT, (
        f"{GATE_CONFIG} does not declare section 10.4's omit list."
    )
    assert gate == base, (
        f"{GATE_CONFIG} differs from {BASE_CONFIG} in more than the omit list: "
        f"{gate!r} against {base!r}. Section 10.4 requires 'the same file plus "
        "an omit list'."
    )


def test_the_subprocess_configuration_is_the_base_run_settings_plus_patching() -> None:
    """Assert the third config differs from the base only as section 10.4 says

    Stated as the same kind of relationship as the case above, so the three files
    read as one table rather than as three unrelated value lists. Two differences
    are expected and no others: the process patching this lane exists for, and
    the absent ``[paths]`` mapping -- which is a real difference, deliberately
    made, and so is asserted rather than left to be noticed.
    """
    base = _resolved_settings(_config_path(BASE_CONFIG))
    subprocess_settings = _resolved_settings(_config_path(SUBPROCESS_CONFIG))

    assert subprocess_settings.pop("parallel", None) is True, (
        f"{SUBPROCESS_CONFIG} does not set parallel; the suffixed data files "
        "its jobs combine would not be written."
    )
    assert subprocess_settings.pop("patch", None) == ["subprocess"], (
        f"{SUBPROCESS_CONFIG} does not patch subprocess, which is the only "
        "reason this configuration exists."
    )
    assert base.pop("paths", None) == EXPECTED_PATHS, (
        f"{BASE_CONFIG} no longer carries section 10.4's paths mapping."
    )

    assert subprocess_settings == base, (
        f"{SUBPROCESS_CONFIG} differs from {BASE_CONFIG}'s [run] settings in "
        f"more than the process patching: {subprocess_settings!r} against "
        f"{base!r}."
    )


def _design_document_blocks() -> dict[str, str]:
    """Extract every ``ini`` block in section 10.4, keyed by the file it names.

    The section is bounded with :func:`tests.test_pytest_configuration._section_body`
    rather than a fresh heading regex; that helper exists because the naive bound
    was measured wrong on this very section, whose blocks open with a ``#``
    comment at column 0 that a heading pattern cannot tell from a heading. A
    second copy here would drift from it.

    Blocks are keyed by the file name in their opening comment rather than by
    position. Section 10.4 holds a shell sample as well as these, so neither
    "the only block" nor "the only ``ini`` block" selects one, and position would
    silently start comparing against the wrong file the day the order changes.

    This imposes a constraint on the document worth stating: **every** ``ini``
    fence in section 10.4 must open with a comment naming a shipped file, so the
    section cannot carry an illustrative ini snippet. That is deliberate -- an
    unnamed block is indistinguishable from a configuration someone forgot to
    ship -- but it is a rule a future author would otherwise meet as a puzzle.

    Returns:
        Each block's contents, keyed by the configuration file it documents.

    Raises:
        AssertionError: When the heading is gone, when a block does not open with
            a comment naming a file, or when two blocks name the same file --
            each of which would silently disable part of the comparison.
    """
    text = TEST_SUITE_PATH.read_text(encoding="utf-8")
    assert DESIGN_SECTION_HEADING in text.splitlines(), (
        f"{TEST_SUITE_PATH.name} no longer contains "
        f"'{DESIGN_SECTION_HEADING}'. This guard parses that section; renaming "
        "it would silently disable the check."
    )
    section = _section_body(text, DESIGN_SECTION_HEADING)
    named: dict[str, str] = {}
    for block in re.findall(r"```ini\n(.*?)```", section, re.DOTALL):
        opening = block.splitlines()[0].split()
        assert opening[:1] == ["#"] and len(opening) > 1, (
            f"An ini block in section 10.4 of {TEST_SUITE_PATH.name} does not "
            f"open with a comment naming its file: {opening!r}"
        )
        assert opening[1] not in named, (
            f"Section 10.4 of {TEST_SUITE_PATH.name} has two ini blocks naming "
            f"{opening[1]}; this guard cannot tell which one ships."
        )
        named[opening[1]] = block
    return named


def _design_document_block(name: str) -> str:
    """Return section 10.4's block for one configuration file.

    Args:
        name: The configuration file the block documents.

    Returns:
        The block's contents, ready to be written out and parsed.

    Raises:
        AssertionError: When the section documents no such file.
    """
    blocks = _design_document_blocks()
    assert name in blocks, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} has no ini block for {name}; "
        f"it documents {sorted(blocks)}."
    )
    return blocks[name]


def test_the_document_and_the_repository_agree_on_which_configurations_exist() -> None:
    """Assert the documented set, the shipped set and this module's table are one set

    The per-file comparisons each answer "does this pair agree?". None of them
    notices a file with no block, a block with no file, or a fourth
    configuration that no case in this module has ever heard of -- all three of
    which leave the document and the tree diverged with the suite green. That is
    the same reason the section bound requires *exactly* one match rather than at
    least one.

    The shipped set is read by globbing rather than from this module's own table,
    so adding a `.coveragerc-something` without adding it to section 10.4 and to
    :data:`EXPECTED_CONFIGURATIONS` fails here.
    """
    documented = set(_design_document_blocks())
    shipped = {path.name for path in REPO_ROOT.glob(".coveragerc*")}
    expected = set(EXPECTED_CONFIGURATIONS)

    assert documented == expected, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} documents {sorted(documented)} "
        f"but this guard knows {sorted(expected)}."
    )
    assert shipped == expected, (
        f"The repository root holds {sorted(shipped)} but section 10.4 "
        f"specifies {sorted(expected)}."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_CONFIGURATIONS))
def test_design_document_declares_the_same_configuration(
    name: str, tmp_path: Path
) -> None:
    """Assert TEST_SUITE.md section 10.4 and the shipped file agree exactly

    The document's block is written to a temporary file and loaded through
    coverage.py, so the comparison is of settings rather than of text: the
    shipped file may explain itself at whatever length is useful, and a
    reformatting that preserves meaning does not fail. Editing either file alone
    turns the suite red, which is what keeps the design document describing the
    configuration that actually runs.

    Args:
        name: The configuration file's name at the repository root.
        tmp_path: pytest's per-test temporary directory.
    """
    documented_path = tmp_path / name
    documented_path.write_text(_design_document_block(name), encoding="utf-8")

    shipped_path = _config_path(name)
    # Bound rather than recomputed inside the assertions: each call builds two
    # configuration objects and edits the environment while it does so.
    documented_resolved = _resolved_settings(documented_path)
    shipped_resolved = _resolved_settings(shipped_path)
    documented_declared = _declared_settings(documented_path)
    shipped_declared = _declared_settings(shipped_path)

    assert documented_resolved == shipped_resolved, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} declares "
        f"{documented_resolved!r} for {name} but the file resolves to "
        f"{shipped_resolved!r}. Change both in the same pull request."
    )
    # Compared as well as the resolved values, so the document and the file
    # cannot agree on a setting coverage would have derived or overridden
    # anyway -- ``parallel`` being exactly that case.
    assert documented_declared == shipped_declared, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} spells out "
        f"{documented_declared!r} for {name} but the file spells out "
        f"{shipped_declared!r}."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_CONFIGURATIONS))
def test_configuration_file_uses_no_option_coverage_does_not_recognise(
    name: str,
) -> None:
    """Assert coverage.py recognises every option the file sets

    coverage.py answers an unknown option with a warning and then ignores it, so
    a single mistyped key leaves a configuration that looks complete in review
    and measures something else at runtime. The warning is the only signal
    available, and nothing in CI would read it, so it is asserted here.

    Args:
        name: The configuration file's name at the repository root.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CoverageWarning)
        coverage.Coverage(config_file=str(_config_path(name)))

    unrecognised = [
        str(w.message) for w in caught if UNRECOGNISED_OPTION in str(w.message)
    ]
    assert not unrecognised, (
        f"coverage.py does not recognise every option in {name}, and ignores "
        f"the ones it does not: {unrecognised}."
    )


def test_a_mistyped_option_is_warned_about_and_then_ignored(tmp_path: Path) -> None:
    """Assert the premise the case above rests on, and the whole module's approach

    The case above can only mean something if coverage.py really does warn about
    an option it does not know, and really does carry on without it. Both halves
    are demonstrated here against a deliberately mistyped file, so "no warnings"
    is a fact about the shipped configurations rather than a fact about a warning
    that never fires.

    The second half is the more important one: ``source_pkg`` is *absent* from
    the resolved settings. That is why every comparison in this module reads
    coverage.py's resolved values instead of the file's text -- a text comparison
    would find this typo identical on both sides and pass.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    mistyped = tmp_path / ".coveragerc"
    mistyped.write_text(f"[run]\nsource_pkg = {PACKAGE}\n", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CoverageWarning)
        settings = _resolved_settings(mistyped)

    assert any(UNRECOGNISED_OPTION in str(w.message) for w in caught), (
        "coverage.py no longer warns about an unknown option, so the "
        "no-warnings case above has stopped being able to fail. "
        f"Warnings seen: {[str(w.message) for w in caught]}"
    )
    assert "source_pkgs" not in settings, (
        "coverage.py accepted a mistyped option name, which would make the "
        f"whole-package view work by accident. Resolved: {settings!r}"
    )


def _child_environment(config_path: Path, data_path: Path) -> dict[str, str]:
    """Build the environment one child coverage run executes under.

    ``COVERAGE_RCFILE`` and ``COVERAGE_FILE`` are absolute, which is section
    10.4's own requirement for every job: configuration discovery walks up from
    the working directory, and the package tests deliberately run from outside
    the checkout where nothing would be found.

    Args:
        config_path: The coveragerc the child must use.
        data_path: Where the child writes its coverage data -- always under the
            test's temporary directory, never into the repository.

    Returns:
        A copy of the current environment, cleaned and pointed at those files.
    """
    environment = dict(os.environ)
    # Inherited from the parent, each of these would silently change the child's
    # run, which is the one thing these cases measure. The coverage startup
    # variables matter for a specific arrangement section 10.4 prescribes: once
    # the subsystem job runs this suite under ``.coveragerc-subprocess``, it
    # exports them, and the installed ``.pth`` would then auto-start a second,
    # differently configured coverage inside every child spawned here.
    for leaked in ("PYTEST_ADDOPTS", *INHERITED_COVERAGE_STARTUP_VARIABLES):
        environment.pop(leaked, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    # The package is imported from the checkout rather than from an install:
    # ``source_pkgs`` resolves the package by importing it, so the child needs it
    # on the path exactly as ``tests/conftest.py`` arranges for this suite.
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    environment["COVERAGE_RCFILE"] = str(config_path)
    environment["COVERAGE_FILE"] = str(data_path)
    return environment


def _run_coverage(
    *args: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run one child ``coverage`` command from the repository root.

    The working directory is the repository root because ``relative_files =
    true`` makes every reported path relative to it; run anywhere else and the
    report keys stop matching what ``git diff`` produces, which is the failure
    the ``[paths]`` mapping exists to prevent.

    Args:
        *args: The coverage subcommand and its arguments.
        environment: The child's environment.

    Returns:
        The completed process, with ``stdout`` and ``stderr`` captured as text.

    Raises:
        subprocess.TimeoutExpired: When the child has not exited within 120
            seconds, so a hung child fails one case instead of the whole run.
    """
    return subprocess.run(
        [sys.executable, "-m", "coverage", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env=environment,
        timeout=120,
        check=False,
    )


def _report_under(config_name: str, probe: Path, tmp_path: Path) -> dict[str, Any]:
    """Measure one probe script under one configuration and return the JSON report.

    Args:
        config_name: The configuration file to measure under.
        probe: The script to run under ``coverage run``.
        tmp_path: The test's temporary directory, which receives every artefact.

    Returns:
        The ``files`` mapping of ``coverage json`` output, keyed by report path.

    Raises:
        AssertionError: When either child command fails, reported with its own
            output so the failure names the coverage error rather than a
            missing file.
    """
    environment = _child_environment(_config_path(config_name), tmp_path / ".coverage")
    run = _run_coverage("run", str(probe), environment=environment)
    assert run.returncode == 0, (
        f"coverage run failed under {config_name}.\n{run.stdout}\n{run.stderr}"
    )

    report_path = tmp_path / "coverage.json"
    report = _run_coverage(
        "json", "-o", str(report_path), environment=environment
    )
    assert report.returncode == 0, (
        f"coverage json failed under {config_name}.\n{report.stdout}\n"
        f"{report.stderr}"
    )
    return _report_files(report_path)


def _report_files(report_path: Path) -> dict[str, Any]:
    """Read a ``coverage json`` report and return its files, keyed POSIX-style.

    The keys come from ``FileReporter.relative_filename()``, which slices the
    **native** absolute path -- so on Windows they are separated by backslashes
    while every expected path in this module is written with forward slashes.
    The ``omit`` patterns themselves are unaffected (coverage matches either
    separator), so this is purely about what the assertions compare against, and
    it matters because section 10.3 schedules the ``subsystem`` marker on Windows
    as well as Linux and ``pyproject.toml`` documents that marker as portable.

    Args:
        report_path: The ``coverage json`` output to read.

    Returns:
        The report's ``files`` mapping with separators normalised.
    """
    files = json.loads(report_path.read_text(encoding="utf-8"))["files"]
    return {key.replace(os.sep, "/"): value for key, value in files.items()}


def _write_probe(tmp_path: Path, body: str) -> Path:
    """Write a probe script into the test's temporary directory.

    Args:
        tmp_path: The test's temporary directory.
        body: The script's source.

    Returns:
        The path written.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(body, encoding="utf-8")
    return probe


@pytest.mark.subsystem
def test_the_base_configuration_reports_an_unexecuted_module_at_zero(
    tmp_path: Path,
) -> None:
    """Assert a module the run never imports is reported, at zero

    This is control 1, and the one observable that proves ``source_pkgs`` is
    doing its job. Under coverage.py's defaults the report contains only files
    that were observed executing, so ``chromium_pool.py`` -- which no test
    imports -- would be absent from the report rather than visibly uncovered,
    and would drag nothing down.

    ``num_statements`` is asserted alongside the zero because a module reported
    with no statements at all would satisfy ``covered_lines == 0`` while proving
    nothing.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    probe = _write_probe(tmp_path, f"import {PROBE_MODULE}\n")

    files = _report_under(BASE_CONFIG, probe, tmp_path)

    assert UNEXECUTED_REPORT_PATH in files, (
        f"{UNEXECUTED_REPORT_PATH} is missing from a report taken under "
        f"{BASE_CONFIG}, so source_pkgs is not in effect and an untested module "
        f"is invisible rather than at zero. Reported: {sorted(files)}"
    )
    summary = files[UNEXECUTED_REPORT_PATH]["summary"]
    assert summary["num_statements"] > 0, (
        f"{UNEXECUTED_REPORT_PATH} is reported with no statements, so the zero "
        "below asserts nothing."
    )
    assert summary["covered_lines"] == 0, (
        f"{UNEXECUTED_REPORT_PATH} is reported as covered by a run that only "
        f"imports {PROBE_MODULE}; this case no longer measures what it claims."
    )


@pytest.mark.subsystem
def test_the_gate_configuration_omits_the_modules_with_no_hermetic_seam(
    tmp_path: Path,
) -> None:
    """Assert the gating view excludes the exempt modules entirely

    The counterpart to the case above, and the reason two configurations exist
    rather than one. Left in the gating view, these modules' zero-hit statements
    would count against ``diff-cover`` and grow the baseline's denominator, so
    ordinary worker development would fail both gates.

    The probe module is asserted still present, as the control: a report that
    failed to produce anything at all would otherwise satisfy every absence
    assertion here.

    ``worker_runner.py`` is deliberately not asserted -- it is the module a later
    step extracts, and does not exist yet.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    probe = _write_probe(tmp_path, f"import {PROBE_MODULE}\n")

    files = _report_under(GATE_CONFIG, probe, tmp_path)

    assert PROBE_REPORT_PATH in files, (
        f"The report taken under {GATE_CONFIG} does not contain "
        f"{PROBE_REPORT_PATH}, so its emptiness -- not the omit list -- is what "
        f"the assertions below would be measuring. Reported: {sorted(files)}"
    )
    for omitted in (UNEXECUTED_REPORT_PATH, EXEMPT_REPORT_PATH):
        assert omitted not in files, (
            f"{omitted} appears in a report taken under {GATE_CONFIG}; section "
            "10.4 classifies it as having no hermetic seam and requires it out "
            "of the gating view."
        )


@pytest.mark.subsystem
def test_the_subprocess_configuration_captures_a_child_process(
    tmp_path: Path,
) -> None:
    """Assert code that runs only in a child process is measured

    The worker runs in a child interpreter, and a child started under ``coverage
    run`` is not instrumented by inheritance -- without ``patch = subprocess``
    its lines are simply never recorded, and the observational report for the
    very modules it was meant to cover reads zero. Measured: under the base
    configuration this same parent produces one data file and a report with
    nothing in it.

    Both halves are asserted. More than one data file proves the child was
    instrumented at all; a module the child imported and the parent did not,
    reported as covered, proves the child's data survived ``coverage combine``.
    A module neither process imported is asserted at zero in the same report, so
    a report that somehow marked everything covered could not pass.

    Three environmental facts this case depends on, recorded because each fails
    it for a reason that has nothing to do with the configuration:

    * ``patch = subprocess`` exports ``COVERAGE_PROCESS_CONFIG``; the ``.pth``
      that reads it, ``a1_coverage.pth``, ships **in the coverage wheel** and is
      run by ``site`` at child startup. Nothing is written at run time, so
      site-packages need not be writable -- but coverage must be *installed*
      into a site-processed site-packages rather than merely reachable on
      ``PYTHONPATH``. Measured: an otherwise identical child started with
      ``-S`` produces one data file instead of two, because ``site`` never runs
      and the hook never fires.
    * The probe spawns its child with **no explicit** ``env=``, so the child
      inherits the variable coverage exported. Passing a dict built before
      coverage patched the environment would leave the child unmeasured and fail
      this case for a reason the message would not explain.
    * Under CI this case runs inside a job that is itself measured, so its
      children are nested one level deeper than usual. The nested-child handling
      that makes that safe is what the 7.10.3 floor in section 10.2 buys.

    All three were measured on Linux. Section 10.3 schedules the ``subsystem``
    marker on Windows too, and 7.10.3 is the floor partly because it fixed
    *Windows* startup failures in this same feature -- so the platform is known
    to matter here. Windows behaviour is therefore unproven until the CI epic
    runs the job there; that is recorded rather than assumed away.

    Unlike the omit list, whose breakage is self-revealing, a broken subprocess
    patch is silent until the epic that first publishes an observational L3
    report -- months of merges away. That gap is why this case is here rather
    than deferred to the job that consumes the configuration.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    child = tmp_path / "child.py"
    child.write_text(f"import {PROBE_MODULE}\n", encoding="utf-8")
    probe = _write_probe(
        tmp_path,
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, {str(child)!r}], check=True)\n",
    )
    environment = _child_environment(
        _config_path(SUBPROCESS_CONFIG), tmp_path / ".coverage"
    )

    run = _run_coverage("run", str(probe), environment=environment)
    assert run.returncode == 0, (
        f"coverage run failed under {SUBPROCESS_CONFIG}.\n{run.stdout}\n"
        f"{run.stderr}"
    )

    data_files = sorted(tmp_path.glob(".coverage*"))
    assert len(data_files) > 1, (
        f"{SUBPROCESS_CONFIG} produced {len(data_files)} data file(s); the "
        "child was not instrumented, so patch = subprocess is not in effect."
    )

    combine = _run_coverage("combine", environment=environment)
    assert combine.returncode == 0, (
        f"coverage combine failed.\n{combine.stdout}\n{combine.stderr}"
    )
    report_path = tmp_path / "coverage.json"
    report = _run_coverage("json", "-o", str(report_path), environment=environment)
    assert report.returncode == 0, (
        f"coverage json failed.\n{report.stdout}\n{report.stderr}"
    )

    files = _report_files(report_path)
    for required in (PROBE_REPORT_PATH, UNEXECUTED_REPORT_PATH):
        assert required in files, (
            f"{required} is missing from the combined report, so the assertions "
            f"below would raise KeyError rather than explain themselves. "
            f"Reported: {sorted(files)}"
        )
    assert files[PROBE_REPORT_PATH]["summary"]["covered_lines"] > 0, (
        f"{PROBE_REPORT_PATH} ran only in the child and is reported uncovered, "
        "so the child's data never reached the combined report."
    )
    assert files[UNEXECUTED_REPORT_PATH]["summary"]["covered_lines"] == 0, (
        f"{UNEXECUTED_REPORT_PATH} was imported by neither process yet is "
        "reported as covered; this report is not measuring what it claims."
    )
