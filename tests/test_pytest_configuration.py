"""Guard the pytest configuration declared in ``pyproject.toml``.

Section 10.5 of ``.system_design/TEST_SUITE.md`` is the policy; the
``[tool.pytest.ini_options]`` table in ``pyproject.toml`` is what the runner
actually reads. This module keeps the two in step, and is the same guard shape
as :mod:`tests.test_dependency_constraints`, which does the job for section
10.2's dependency table.

Every check here exists because the failure it catches is **silent**. An
unregistered marker without ``strict_markers`` is accepted with a warning, and
the test it labels is then excluded from every marker-selected CI job while
still reporting as "passing". A configuration file that pytest never loaded --
because a stray ``pytest.ini`` outranks ``pyproject.toml`` -- leaves the suite
running unconfigured while every assertion about the file's *contents* stays
green. Neither turns anything red on its own, so something has to assert them.

The checks come in two kinds, and both are needed:

* those reading ``pyproject.toml`` with :mod:`tomllib`, which pin what the
  repository declares -- including that it declares nothing extra;
* those reading :class:`~pytest.Config` through the ``pytestconfig`` fixture,
  which pin what the **running** pytest actually loaded.

Neither kind implies the other.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TEST_SUITE_PATH = REPO_ROOT / ".system_design" / "TEST_SUITE.md"

DESIGN_SECTION_HEADING = "### 10.5 pytest configuration"

# Section 10.5 of ``.system_design/TEST_SUITE.md``, in the order that section
# lists them. The order is asserted along with the contents: it makes a drift
# between the document and ``pyproject.toml`` a one-line diff to read rather
# than a set comparison to reason about.
EXPECTED_MARKERS: dict[str, str] = {
    "live": "hits the real network; needs KINDLY_RUN_LIVE_TESTS=1",
    "subsystem": "needs a real socket or child process; portable",
    "chromium": "needs a real browser; Linux container only",
    "package": "runs against an installed wheel, not the source tree",
    "serper": "live test requiring SERPER_API_KEY",
    "extraction": "live content-extraction canary; browser, no credential",
    "slow": "over a second",
}

# The rest of section 10.5's block. Kept separate from the markers because
# pytest's own ``markers`` ini value is additive at runtime -- plugins append to
# it -- so the two are asserted against the runner in different ways.
EXPECTED_SETTINGS: dict[str, object] = {
    "testpaths": ["tests"],
    "strict_markers": True,
    "strict_config": True,
    "asyncio_mode": "auto",
    "asyncio_default_fixture_loop_scope": "function",
}

# What pytest reports for a marker it does not know. Asserted in pytest's own
# wording rather than a paraphrase, so a release that changes the message fails
# here loudly instead of quietly weakening the check to "some error occurred".
UNKNOWN_MARKER_ERROR = "not found in `markers` configuration option"

# ``pytest.ExitCode.INTERRUPTED``, which is what an aborted collection returns.
# Deliberately distinguished from 1 (``TESTS_FAILED``): a test that merely fails
# is not the outcome being asserted.
EXIT_INTERRUPTED = 2


def _pyproject_ini_options() -> dict:
    """Read the ``[tool.pytest.ini_options]`` table out of ``pyproject.toml``.

    Returns:
        The table's keys and values, as declared in the file.

    Raises:
        AssertionError: When the table is absent, which is the state this whole
            module exists to prevent.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        document = tomllib.load(handle)
    table = document.get("tool", {}).get("pytest", {}).get("ini_options")
    assert table is not None, (
        f"{PYPROJECT_PATH.name} declares no [tool.pytest.ini_options] table. "
        "Section 10.5 of TEST_SUITE.md requires one."
    )
    return table


def _expected_marker_entries() -> list[str]:
    """Render :data:`EXPECTED_MARKERS` in pytest's ``name: description`` form.

    Returns:
        One entry per marker, in section 10.5's order.
    """
    return [f"{name}: {description}" for name, description in EXPECTED_MARKERS.items()]


def _expected_ini_options() -> dict:
    """Assemble the whole table section 10.5 requires.

    Returns:
        :data:`EXPECTED_SETTINGS` plus the rendered ``markers`` list.
    """
    return {**EXPECTED_SETTINGS, "markers": _expected_marker_entries()}


def _run_pytest(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a child pytest against the **committed** ``pyproject.toml``.

    ``-c`` points the child at this repository's real configuration rather than
    at a copy written into a temporary directory. That distinction is the point:
    a test that builds its own config file proves only that pytest works, not
    that the configuration this repository ships does.

    Args:
        *args: Arguments appended after the configuration flags.
        cwd: Working directory for the child, kept off the repository tree so
            the run cannot pick up ``tests/conftest.py`` or write into it.

    Returns:
        The completed process, with ``stdout`` and ``stderr`` captured as text.

    Raises:
        subprocess.TimeoutExpired: When the child has not exited within 120
            seconds. A bounded wait rather than an unbounded one, so a child
            that hangs fails the case instead of the run.
    """
    # ``PYTEST_ADDOPTS`` from the parent environment would silently change the
    # child's behaviour, so the child gets a cleaned copy.
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
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
        cwd=cwd,
        env=environment,
        timeout=120,
        check=False,
    )


def _write_marked_test(directory: Path, marker: str) -> Path:
    """Write a one-case test module carrying ``marker``.

    Args:
        directory: Where to write the module.
        marker: The marker name to apply.

    Returns:
        The path written.
    """
    path = directory / "test_marked.py"
    path.write_text(
        "import pytest\n"
        "\n"
        "\n"
        f"@pytest.mark.{marker}\n"
        "def test_marked() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return path


def test_configuration_source_is_pyproject(pytestconfig: pytest.Config) -> None:
    """Assert the running pytest loaded this repository's ``pyproject.toml``

    pytest's precedence is ``pytest.ini`` > ``pyproject.toml`` > ``tox.ini`` >
    ``setup.cfg``, so a single stray ``pytest.ini`` at the repository root takes
    the whole block below out of service. Every check that reads
    ``pyproject.toml`` directly would still pass, and the suite would run
    unconfigured. This is the case that notices.

    Args:
        pytestconfig: The session's configuration, injected by pytest.
    """
    # Both sides are resolved before comparing. ``inipath`` is built with
    # ``absolutepath()``, which does not expand symlinks or Windows 8.3 short
    # names, so comparing spellings would fail on a symlinked checkout while
    # reporting a configuration takeover that had not happened.
    inipath = pytestconfig.inipath
    assert inipath is not None and inipath.resolve() == PYPROJECT_PATH, (
        f"pytest loaded its configuration from {inipath}, not from "
        f"{PYPROJECT_PATH}. A higher-precedence configuration file has taken "
        "over and the [tool.pytest.ini_options] block is not in effect."
    )


@pytest.mark.parametrize(("option", "value"), sorted(EXPECTED_SETTINGS.items()))
def test_runner_loaded_expected_setting(
    pytestconfig: pytest.Config, option: str, value: object
) -> None:
    """Assert one section 10.5 setting reached the runner, not merely the file

    Parametrized one setting per case so five broken settings report as five
    failures rather than one.

    Args:
        pytestconfig: The session's configuration, injected by pytest.
        option: The ini option name.
        value: The value section 10.5 requires.
    """
    assert pytestconfig.getini(option) == value, (
        f"pytest reports {option} = {pytestconfig.getini(option)!r}; section "
        f"10.5 of TEST_SUITE.md requires {value!r}."
    )


@pytest.mark.parametrize(("name", "description"), sorted(EXPECTED_MARKERS.items()))
def test_runner_registered_expected_marker(
    pytestconfig: pytest.Config, name: str, description: str
) -> None:
    """Assert one section 10.5 marker is registered with its documented text

    Asserted as containment rather than equality: pytest's ``markers`` value is
    additive at runtime, and the installed plugins and pytest's own built-ins
    append to it. The "and nothing else" half of this claim is
    :func:`test_pyproject_declares_exactly_the_expected_ini_options`, which
    reads the file.

    Args:
        pytestconfig: The session's configuration, injected by pytest.
        name: The marker name.
        description: The description section 10.5 gives it.
    """
    registered = pytestconfig.getini("markers")
    assert f"{name}: {description}" in registered, (
        f"pytest does not report marker '{name}' as '{name}: {description}'. "
        "Section 10.5 of TEST_SUITE.md defines it; this is what "
        "'pytest --markers' prints."
    )


def test_pyproject_declares_exactly_the_expected_ini_options() -> None:
    """Assert ``pyproject.toml`` declares section 10.5's table and nothing more

    The other direction of the two cases above. Without it, a setting or marker
    could be added to ``pyproject.toml`` alone and the design document would
    stop describing the configuration CI actually runs under.
    """
    assert _pyproject_ini_options() == _expected_ini_options(), (
        f"{PYPROJECT_PATH.name} declares {_pyproject_ini_options()!r}, but "
        f"section 10.5 of TEST_SUITE.md defines {_expected_ini_options()!r}."
    )


def test_strict_markers_is_not_declared_through_addopts() -> None:
    """Reject the ``addopts`` spelling of strictness, which pytest 9.0 ignores

    pytest 9.0.0 through 9.0.3 **silently ignore** ``--strict-markers`` and
    ``--strict-config`` when they arrive through ``addopts``
    (`pytest#14442 <https://github.com/pytest-dev/pytest/issues/14442>`_, fixed
    in 9.1.0). Section 10.2's bound is ``pytest>=9,<10``, so those releases are
    installable, and on them the ``addopts`` form leaves an unregistered marker
    passing with nothing but a warning. Measured across 9.0.0, 9.0.1, 9.0.2,
    9.0.3, 9.1.0 and 9.1.1: every 9.0.x exits 0 under the ``addopts`` form,
    where ``strict_markers = true`` exits 2 on all six.

    The two spellings are therefore not interchangeable here, and moving the
    setting back into ``addopts`` would be a silent regression on any allowed
    pytest below 9.1.0 -- which the child-process cases below cannot catch,
    because they run on whatever pytest CI resolved, where both spellings work.
    """
    addopts = _pyproject_ini_options().get("addopts", "")
    entries = addopts if isinstance(addopts, list) else addopts.split()
    offenders = [entry for entry in entries if entry.startswith("--strict")]
    assert not offenders, (
        f"{PYPROJECT_PATH.name} declares {offenders} in addopts. pytest 9.0.0 "
        "through 9.0.3 ignore strictness flags passed that way (pytest#14442); "
        "use the strict_markers / strict_config ini options instead."
    )


async def test_asyncio_auto_mode_awaits_a_plain_async_test() -> None:
    """Assert ``asyncio_mode = "auto"`` is in effect, by depending on it

    Deliberately an unmarked ``async def``. Section 10.1 standardizes on
    pytest-asyncio in auto mode because the project is pure asyncio and the
    ``asyncio`` marker is noise; this case is what makes that observable rather
    than merely declared. Measured on pytest 9.1.1: with the mode unset, or with
    the plugin not loaded, this fails with ``async def functions are not
    natively supported``; with auto mode it passes.

    That failure is loud, which is the point -- the setting reaching the runner
    is asserted by :func:`test_runner_loaded_expected_setting`, and this asserts
    the runner does something with it.
    """
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().is_running()


def _section_body(text: str, heading: str) -> str:
    """Return the lines of one Markdown section, ignoring fenced code blocks.

    Bounded by walking lines and tracking fence state rather than by splitting
    on a heading pattern. A regex cannot tell a heading from a ``#`` comment at
    column 0 inside a fence, and this document is full of both -- section 10.4's
    samples open with ``# tests/conftest.py`` and ``# .coveragerc``, and section
    10.5's own prose invites a TOML comment inside its block. Splitting on the
    pattern would truncate the section at that comment, leaving the closing
    fence outside it and failing with a fence count that points away from the
    cause.

    Args:
        text: The whole document.
        heading: The exact heading line the section starts at.

    Returns:
        Everything after ``heading`` up to the next heading of level 1-3 that is
        not inside a fence, or to the end of the document.
    """
    lines = text.splitlines()
    start = lines.index(heading) + 1
    body: list[str] = []
    fenced = False
    for line in lines[start:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            break
        body.append(line)
    return "\n".join(body)


def _design_document_ini_options() -> dict:
    """Parse section 10.5's TOML block out of ``TEST_SUITE.md``.

    The block is read as TOML rather than scraped line by line, so a formatting
    change to the document that preserves its meaning does not fail the
    comparison. The search is bounded to the section by :func:`_section_body`
    and requires exactly one fenced block inside it: an unbounded search would
    silently start comparing against some later section's TOML the day one is
    added.

    Returns:
        The ``[tool.pytest.ini_options]`` table the design document declares.

    Raises:
        AssertionError: When the heading, or exactly one fenced block under it,
            cannot be found.
    """
    text = TEST_SUITE_PATH.read_text(encoding="utf-8")
    assert DESIGN_SECTION_HEADING in text.splitlines(), (
        f"{TEST_SUITE_PATH.name} no longer contains '{DESIGN_SECTION_HEADING}'. "
        "This guard parses that section; renaming it would silently disable the "
        "check."
    )
    section = _section_body(text, DESIGN_SECTION_HEADING)
    blocks = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1, (
        f"Section 10.5 of {TEST_SUITE_PATH.name} contains {len(blocks)} fenced "
        "```toml blocks; this guard requires exactly one."
    )
    table = (
        tomllib.loads(blocks[0]).get("tool", {}).get("pytest", {}).get("ini_options")
    )
    assert table is not None, (
        f"Section 10.5's TOML block in {TEST_SUITE_PATH.name} declares no "
        "[tool.pytest.ini_options] table."
    )
    return table


def test_design_document_declares_the_same_configuration() -> None:
    """Assert TEST_SUITE.md section 10.5 and ``pyproject.toml`` agree exactly

    Compared as parsed tables, key for key, in both directions. Comments are
    invisible to the parser, so ``pyproject.toml`` may explain itself at
    whatever length is useful. Editing either file alone turns the suite red,
    which is what keeps the design document describing the configuration that
    actually runs rather than the one it was written against.
    """
    assert _design_document_ini_options() == _pyproject_ini_options(), (
        f"Section 10.5 of {TEST_SUITE_PATH.name} declares "
        f"{_design_document_ini_options()!r} but {PYPROJECT_PATH.name} declares "
        f"{_pyproject_ini_options()!r}. Change both in the same pull request."
    )


# A miniature document exercising both bounds at once: a ``#`` comment at column
# 0 inside the fence, which must NOT end the section, and a following heading,
# which must.
_SECTION_FIXTURE = """\
### 10.5 pytest configuration

```toml
# a comment the section bound must not trip on
[tool.pytest.ini_options]
testpaths = ["tests"]
```

Trailing prose.

## 11. Next section

```toml
[tool.something.else]
ignored = true
```
"""


def test_section_bound_ignores_a_comment_inside_the_fence() -> None:
    """Assert a ``#`` comment in the block does not truncate the section

    Section 10.5's prose invites comments, and a heading-shaped regex cannot
    tell one from a heading. Getting this wrong fails the guard with a fence
    count that points away from the cause, so it is asserted directly.
    """
    section = _section_body(_SECTION_FIXTURE, DESIGN_SECTION_HEADING)

    assert "# a comment the section bound must not trip on" in section
    assert "Trailing prose." in section


def test_section_bound_stops_at_the_next_heading() -> None:
    """Assert the section really is bounded -- the control for the case above

    Without this, :func:`_section_body` could satisfy the previous case by never
    terminating at all, and the guard would start comparing against whatever
    TOML a later section grows.
    """
    section = _section_body(_SECTION_FIXTURE, DESIGN_SECTION_HEADING)

    assert "## 11. Next section" not in section
    assert "tool.something.else" not in section
    assert len(re.findall(r"```toml\n(.*?)```", section, re.DOTALL)) == 1


@pytest.mark.subsystem
def test_unregistered_marker_fails_collection(tmp_path: Path) -> None:
    """Assert an unregistered marker aborts collection

    This is the behaviour ``strict_markers`` is set for, as opposed to the
    setting itself, which :func:`test_runner_loaded_expected_setting` asserts.
    Turning the option off leaves that case failing on the declaration and this
    one failing on the consequence, which is the pair worth having.

    Marked ``subsystem`` because it spawns a child process, which is what
    section 10.5 defines that marker to mean. It needs nothing else -- no
    network, no browser, no credential.

    Args:
        tmp_path: pytest's per-test temporary directory, kept off the
            repository tree.
    """
    _write_marked_test(tmp_path, "definitely_not_a_registered_marker")

    result = _run_pytest("-q", "test_marked.py", cwd=tmp_path)

    assert result.returncode == EXIT_INTERRUPTED, (
        f"Expected exit {EXIT_INTERRUPTED} (collection interrupted), got "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )
    assert UNKNOWN_MARKER_ERROR in result.stdout, (
        "Collection failed, but not for the unregistered marker.\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.subsystem
def test_registered_marker_is_collected(tmp_path: Path) -> None:
    """Assert a registered marker still runs -- the control for the case above

    Without this, :func:`test_unregistered_marker_fails_collection` would pass
    against a configuration that broke collection for any reason at all. It was
    the only user of the ``slow`` marker until
    :func:`tests.test_baseline_failure_ledger.test_live_outcome_matches_the_ledger`
    earned one by spawning a child run of the whole suite; it remains the only
    synthetic one.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    _write_marked_test(tmp_path, "slow")

    result = _run_pytest("-q", "test_marked.py", cwd=tmp_path)

    assert result.returncode == 0, (
        f"A registered marker should collect and pass, got exit "
        f"{result.returncode}.\n{result.stdout}\n{result.stderr}"
    )
    assert "1 passed" in result.stdout, (
        f"Expected one passing test.\n{result.stdout}\n{result.stderr}"
    )
