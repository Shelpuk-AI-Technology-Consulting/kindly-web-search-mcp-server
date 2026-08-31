"""Guard the three coverage configurations declared in ``.coveragerc*``.

Section 10.4 of ``.system_design/TEST_SUITE.md`` is the policy; ``.coveragerc``,
``.coveragerc-gate`` and ``.coveragerc-subprocess`` are what coverage.py actually
reads. This module keeps the two in step, in the same guard shape as
:mod:`tests.test_pytest_configuration` and :mod:`tests.test_min_selected_guard`.

Two things about this module's assertions are deliberate and load-bearing:

* **Every configuration is read through coverage.py's own parser, never through
  :mod:`configparser`.** Measured on coverage 7.16.0: an unrecognised key in a
  coveragerc -- ``source_pkg`` for ``source_pkgs``, a misspelled ``patch`` --
  produces a :class:`~coverage.exceptions.CoverageWarning` and is *otherwise
  ignored*. The run continues with that setting silently absent. A text
  comparison sees the typo in both the document and the file, finds them
  identical, and passes, while the coverage run it was meant to protect measures
  the wrong thing. Only the value coverage.py *resolved* can fail on that typo.
* **Each configuration is compared as its whole difference from coverage's
  defaults**, not as a list of keys someone remembered to check. An option added
  to a shipped file that this module does not name would otherwise take effect
  unnoticed.

The three behavioural cases at the end run real coverage in a child process,
because section 10.4's central claim -- that a module no test imports is reported
at zero rather than being absent -- is a claim about what coverage.py *does*, and
no amount of reading the configuration file can establish it.
"""

from __future__ import annotations

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

# Section 10.4's ``[paths]`` mapping. Present in the two gating configurations
# and deliberately absent from the subprocess one: the mapping matters only where
# a wheel is involved, and the observational subsystem jobs run from a source
# checkout.
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

# The module the probe imports. Any module the package can import without side
# effects would do; this one is a plain dataclass module with no I/O.
PROBE_MODULE = f"{PACKAGE}.models"
PROBE_REPORT_PATH = f"src/{PACKAGE}/models.py"

# The module with no test file anywhere -- 213 statements, and the concrete gap
# that made control 1 necessary. It is the observable for both the whole-package
# claim and the exemption claim.
UNEXECUTED_REPORT_PATH = f"src/{PACKAGE}/scrape/chromium_pool.py"
EXEMPT_REPORT_PATH = f"src/{PACKAGE}/scrape/nodriver_worker.py"


def _config_path(name: str) -> Path:
    """Return a shipped configuration file's path, asserting it is there.

    Checked here rather than left to coverage.py, which answers a missing file
    with a :class:`~coverage.exceptions.ConfigError` traceback. Every case in
    this module is meaningless without the file, so all of them reach it through
    this helper and fail with one sentence naming the document that requires it.

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

    Returns:
        Every non-default setting, keyed by coverage.py's own attribute name.
    """
    # The baseline is read from a file rather than from ``config_file=False`` so
    # that both sides go down the identical code path; only the contents differ.
    defaults = coverage.Coverage(config_file=os.devnull).config
    actual = coverage.Coverage(config_file=str(config_path)).config
    return {
        name: value
        for name, value in vars(actual).items()
        if not name.startswith("_")
        and name not in PROVENANCE_ATTRIBUTES
        and value != getattr(defaults, name, None)
    }


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


def _design_document_block(name: str) -> str:
    """Extract one of section 10.4's three ``ini`` blocks from ``TEST_SUITE.md``.

    The section is bounded with :func:`tests.test_pytest_configuration._section_body`
    rather than a fresh heading regex; that helper exists because the naive bound
    was measured wrong on this very section, whose blocks open with a ``#``
    comment at column 0. A second copy here would drift from it.

    Within the section the block is chosen by the file name in its opening
    comment. Section 10.4 holds four fenced blocks -- these three and a shell
    sample -- so neither "the only block" nor "the only ``ini`` block" selects
    one, and position would silently start comparing against the wrong file the
    day the order changes.

    Args:
        name: The configuration file the block documents.

    Returns:
        The block's contents, ready to be written out and parsed.

    Raises:
        AssertionError: When the heading, or exactly one block naming that file,
            cannot be found -- either of which would silently disable the
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
        for block in re.findall(r"```ini\n(.*?)```", section, re.DOTALL)
        if block.splitlines()[0].split()[1:2] == [name]
    ]
    assert len(blocks) == 1, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} contains {len(blocks)} ini "
        f"blocks whose opening comment names {name}; this guard requires "
        "exactly one."
    )
    return blocks[0]


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

    documented = _resolved_settings(documented_path)
    shipped = _resolved_settings(_config_path(name))

    assert documented == shipped, (
        f"Section 10.4 of {TEST_SUITE_PATH.name} declares {documented!r} for "
        f"{name} but the file resolves to {shipped!r}. Change both in the same "
        "pull request."
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

    unrecognised = [str(w.message) for w in caught if "Unrecognized" in str(w.message)]
    assert not unrecognised, (
        f"coverage.py does not recognise every option in {name}, and ignores "
        f"the ones it does not: {unrecognised}."
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
    # Inherited from the parent this would silently change the child's run,
    # which is the one thing these cases measure.
    environment.pop("PYTEST_ADDOPTS", None)
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
    return json.loads(report_path.read_text(encoding="utf-8"))["files"]


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
    very modules it was meant to cover reads zero. Measured on coverage 7.16.0:
    under the base configuration this same parent produces one data file and a
    report with nothing in it.

    Both halves are asserted. More than one data file proves the child was
    instrumented at all; a module the child imported and the parent did not,
    reported as covered, proves the child's data survived ``coverage combine``.
    A module neither process imported is asserted at zero in the same report, so
    a report that somehow marked everything covered could not pass.

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

    files = json.loads(report_path.read_text(encoding="utf-8"))["files"]
    assert files[PROBE_REPORT_PATH]["summary"]["covered_lines"] > 0, (
        f"{PROBE_REPORT_PATH} ran only in the child and is reported uncovered, "
        "so the child's data never reached the combined report."
    )
    assert files[UNEXECUTED_REPORT_PATH]["summary"]["covered_lines"] == 0, (
        f"{UNEXECUTED_REPORT_PATH} was imported by neither process yet is "
        "reported as covered; this report is not measuring what it claims."
    )
