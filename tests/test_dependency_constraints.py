"""Guard the dependency bounds declared in ``pyproject.toml``.

The documented install path is ``uvx --from git+https://...``, which re-resolves
dependencies from PyPI on every start and ignores ``uv.lock``. The bounds that
reach users therefore live in ``pyproject.toml``, not in the lock file, so they
are asserted here.

The rest of the suite covers the other half of this contract: six test modules
import ``kindly_web_search_mcp_server.server``, so an SDK that no longer provides
``mcp.server.fastmcp`` fails the suite. Those imports prove the *resolved* version
works; the assertions here constrain what may be resolved in the first place.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TEST_SUITE_PATH = REPO_ROOT / ".system_design" / "TEST_SUITE.md"
RATCHET_LOCKFILE_PATH = REPO_ROOT / "requirements-ratchet.txt"

# The MCP SDK removed ``mcp.server.fastmcp`` in 2.0.0. The later versions are
# listed deliberately: a too-narrow pin such as ``!=2.0.0`` excludes exactly the
# one release while leaving 2.1.0 and 3.0.0 installable, which would reintroduce
# the outage while still satisfying a check that only tried 2.0.0.
REJECTED_MCP_VERSIONS = ("2.0.0", "2.1.0", "3.0.0")

# Matches the lower bound declared in ``pyproject.toml``. Raising that floor
# should update this too; the pairing is what makes an accidental
# over-tightening visible instead of silent.
SUPPORTED_MCP_VERSION = "1.25.0"


# Imported directly at the top of ``server.py`` (``import uvicorn``,
# ``from starlette.middleware.cors import CORSMiddleware``). Both are satisfied
# transitively through ``mcp`` today, so nothing fails until the SDK drops one of them
# — at which point the documented ``uvx --from git+https://...`` install breaks at
# import for every user. Declaring them turns that into a resolver error instead.
DIRECTLY_IMPORTED_DEPENDENCIES = ("starlette", "uvicorn")


# section 10.2 of ``.system_design/TEST_SUITE.md``, split across the extras that carry it.
# The design document is the policy; this table is what ``pyproject.toml`` is
# measured against, and ``test_design_table_matches_expected_bounds`` is what stops
# the two drifting apart. Every entry carries a specifier, and every entry whose
# upstream has a major-version cadence carries an upper bound too, because this
# project has twice been broken by a dependency that had neither.
EXPECTED_EXTRA_BOUNDS: dict[str, dict[str, str]] = {
    "dev": {
        "pytest": ">=9,<10",
        "pytest-asyncio": ">=1.4,<2",
        "hypothesis": ">=6.167,<7",
        "coverage": ">=7.10.3,<8",
        "diff-cover": ">=10.4,<11",
        "ruff": ">=0.6,<1",
        "mypy": ">=2.3.1,<3",
        "packaging": ">=24",
    },
    # ``mutmut`` needs ``fork()`` and is therefore Linux-only. It lives in its own
    # extra so the nightly can install it without making ``dev`` - which everyone
    # installs - carry a tool that cannot work on Windows.
    "mutation": {
        "mutmut": ">=3,<4",
    },
    # The pinned lane of section 10.4. ``coverage`` is exact here and range-bounded in
    # ``dev``: this is the environment the committed coverage baseline is measured
    # in, so a patch release that moves a two-decimal total would break the ratchet
    # for reasons that have nothing to do with the change under test.
    "ratchet": {
        "pytest": ">=9,<10",
        "pytest-asyncio": ">=1.4,<2",
        "hypothesis": ">=6.167,<7",
        "coverage": "==7.13.5",
        "diff-cover": ">=10.4,<11",
        "mypy": ">=2.3.1,<3",
        "packaging": ">=24",
    },
}

# One case per tool per extra, so a removed or loosened bound fails exactly one
# test and the failure names the extra it came from.
EXTRA_BOUND_CASES = [
    (extra, name, specifier)
    for extra, bounds in EXPECTED_EXTRA_BOUNDS.items()
    for name, specifier in bounds.items()
]

# A `pip freeze` run inside a directory containing the project records it as a
# local path or URL - ``kindly-web-search-mcp-server @ file:///...`` - which no
# other machine can resolve. These are the prefixes and infixes that betray it.
UNRESOLVABLE_LOCKFILE_MARKERS = ("git+", "hg+", "svn+", "bzr+", "file:", " @ ", "-e ")

# `pip freeze` emits bare `name==version` lines. Anything else in the ratchet
# lockfile - a range, a marker, a URL - means the environment it was taken from
# was not the clean one section 10.4 describes. If that section's `--require-hashes`
# option is ever adopted, relax this in the same PR: hashed output is
# `name==version \` followed by indented `--hash=` continuation lines.
PIN_PATTERN = re.compile(r"^[A-Za-z0-9._-]+==[^\s;]+$")

# Lowercased substrings the lockfile header must carry. A lockfile regenerated on
# the wrong platform or Python version silently moves the coverage baseline, so
# the file has to say which one produced it. Each token is specific enough to
# distinguish the right procedure from a plausible wrong one: a bare "ratchet"
# would be satisfied by the filename on line 1, a bare "3.13" by any prose
# mentioning the interpreter, and a bare ".[ratchet]" by the paragraph below the
# command that also names the extra. The install token is therefore the whole
# command line, so pointing it at the wrong extra fails here.
REGENERATION_HEADER_TOKENS = (
    "pip install '.[ratchet]'",
    "python3.13",
    "linux",
    "pip freeze --exclude-editable",
)

# Where the policy this module enforces is written down.
DESIGN_TABLE_HEADING = "### 10.2 Dependencies and version policy"
BACKTICKED_PATTERN = re.compile(r"`([^`]+)`")


def _declared_dependency_names() -> set[str]:
    """Collect the distribution names declared in ``[project].dependencies``

    Returns:
        The lowercased names of every declared runtime dependency.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        pyproject = tomllib.load(handle)

    return {
        Requirement(entry).name.lower() for entry in pyproject["project"]["dependencies"]
    }


@pytest.mark.parametrize("name", DIRECTLY_IMPORTED_DEPENDENCIES)
def test_directly_imported_packages_are_declared(name: str) -> None:
    """Declare every package ``server.py`` imports without going through ``mcp``"""
    assert name in _declared_dependency_names(), (
        f"pyproject.toml does not declare '{name}', which server.py imports directly. "
        "It currently resolves transitively via 'mcp'; that is not a guarantee."
    )


def _read_mcp_requirement() -> Requirement:
    """Extract the ``mcp`` requirement declared in ``pyproject.toml``

    Returns:
        The parsed requirement for the MCP Python SDK.

    Raises:
        AssertionError: If no ``mcp`` entry is declared.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        pyproject = tomllib.load(handle)

    for entry in pyproject["project"]["dependencies"]:
        requirement = Requirement(entry)
        if requirement.name.lower() == "mcp":
            return requirement

    raise AssertionError("pyproject.toml declares no 'mcp' dependency")


@pytest.fixture(scope="module")
def mcp_requirement() -> Requirement:
    """Return the ``mcp`` requirement declared in ``pyproject.toml``"""
    return _read_mcp_requirement()


@pytest.mark.parametrize("version", REJECTED_MCP_VERSIONS)
def test_mcp_requirement_rejects_versions_without_fastmcp(
    mcp_requirement: Requirement, version: str
) -> None:
    """Reject every MCP SDK release that dropped ``mcp.server.fastmcp``"""
    assert not mcp_requirement.specifier.contains(Version(version)), (
        f"pyproject.toml allows mcp {version}, which has no 'mcp.server.fastmcp' "
        f"and breaks server.py at import time. Declared specifier: '{mcp_requirement}'."
    )


def test_mcp_requirement_still_allows_supported_version(
    mcp_requirement: Requirement,
) -> None:
    """Keep the supported MCP SDK version installable"""
    assert mcp_requirement.specifier.contains(Version(SUPPORTED_MCP_VERSION)), (
        f"pyproject.toml no longer allows the supported mcp {SUPPORTED_MCP_VERSION}. "
        f"Declared specifier: '{mcp_requirement}'."
    )


def _project_name() -> str:
    """Read the distribution name declared in ``pyproject.toml``

    Returns:
        The project's own distribution name.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        return tomllib.load(handle)["project"]["name"]


def _raw_extra_entries(extra: str) -> list[str]:
    """Read one extra's requirement strings verbatim

    Args:
        extra: Name of the extra declared under ``[project.optional-dependencies]``.

    Returns:
        The declared requirement strings, in declaration order. An extra that is
        not declared at all yields an empty list rather than raising, so the
        assertion that fails is the readable one about a missing package instead
        of a bare ``KeyError``.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        pyproject = tomllib.load(handle)

    return pyproject["project"].get("optional-dependencies", {}).get(extra, [])


def _extra_requirements(extra: str) -> dict[str, Requirement]:
    """Parse one extra's requirements, keyed by canonical distribution name

    Args:
        extra: Name of the extra declared under ``[project.optional-dependencies]``.

    Returns:
        A mapping of canonical package name to parsed requirement. Duplicate
        declarations collapse here, which is why they are asserted against
        separately.
    """
    return {
        canonicalize_name(requirement.name): requirement
        for requirement in (Requirement(entry) for entry in _raw_extra_entries(extra))
    }


@pytest.mark.parametrize(
    ("extra", "name", "specifier"),
    EXTRA_BOUND_CASES,
    ids=[f"{extra}-{name}" for extra, name, _ in EXTRA_BOUND_CASES],
)
def test_extra_declares_expected_bound(extra: str, name: str, specifier: str) -> None:
    """Declare each section 10.2 tool in its extra with exactly the documented bound"""
    declared = _extra_requirements(extra)
    canonical = canonicalize_name(name)

    assert canonical in declared, (
        f"pyproject.toml's '{extra}' extra does not declare '{name}'. Section 10.2 of "
        f"TEST_SUITE.md requires it at '{name}{specifier}'."
    )
    assert declared[canonical].specifier == SpecifierSet(specifier), (
        f"pyproject.toml declares '{declared[canonical]}' in the '{extra}' extra, "
        f"but section 10.2 of TEST_SUITE.md requires '{name}{specifier}'."
    )


@pytest.mark.parametrize("extra", sorted(EXPECTED_EXTRA_BOUNDS))
def test_extra_declares_exactly_the_expected_packages(extra: str) -> None:
    """Keep each tooling extra to exactly the packages section 10.2 assigns it"""
    declared = set(_extra_requirements(extra))
    expected = {canonicalize_name(name) for name in EXPECTED_EXTRA_BOUNDS[extra]}

    assert declared == expected, (
        f"pyproject.toml's '{extra}' extra declares {sorted(declared)}, but section 10.2 "
        f"of TEST_SUITE.md assigns it {sorted(expected)}. Unexpected: "
        f"{sorted(declared - expected)}; missing: {sorted(expected - declared)}."
    )


@pytest.mark.parametrize("extra", sorted(EXPECTED_EXTRA_BOUNDS))
def test_extra_declares_each_package_once(extra: str) -> None:
    """Refuse a duplicate declaration, which would hide an unbounded entry"""
    entries = _raw_extra_entries(extra)
    names = [canonicalize_name(Requirement(entry).name) for entry in entries]
    duplicated = sorted({name for name in names if names.count(name) > 1})

    assert not duplicated, (
        f"pyproject.toml's '{extra}' extra declares {duplicated} more than once. "
        "The last entry wins at resolve time, so a duplicate is a way to smuggle "
        "an unbounded requirement past every other check in this module."
    )


@pytest.mark.parametrize("extra", sorted(EXPECTED_EXTRA_BOUNDS))
def test_extra_dependencies_have_no_environment_marker(extra: str) -> None:
    """Install the same tooling on every platform the suite runs on"""
    conditional = sorted(
        f"{name}; {requirement.marker}"
        for name, requirement in _extra_requirements(extra).items()
        if requirement.marker is not None
    )

    assert not conditional, (
        f"pyproject.toml's '{extra}' extra declares {conditional} behind an "
        "environment marker, so it silently vanishes on the platforms the marker "
        "excludes. The suite runs on Windows and Linux and must get the same "
        "tools on both; a platform-specific tool belongs in its own extra, the "
        "way 'mutmut' does."
    )


@pytest.mark.parametrize("extra", sorted(EXPECTED_EXTRA_BOUNDS))
def test_extra_dependencies_are_all_bounded(extra: str) -> None:
    """Refuse an unbounded entry in any tooling extra"""
    unbounded = sorted(
        name for name, requirement in _extra_requirements(extra).items()
        if len(requirement.specifier) == 0
    )

    assert not unbounded, (
        f"pyproject.toml's '{extra}' extra declares {unbounded} without a version "
        "bound. This project has twice been broken by an unbounded dependency, "
        "which is why section 10.2 states bounds rather than 'current'."
    )


def _ratchet_lockfile_text() -> str:
    """Read the committed ratchet lockfile

    Returns:
        The full text of ``requirements-ratchet.txt``.

    Raises:
        AssertionError: If the lockfile has not been generated.
    """
    assert RATCHET_LOCKFILE_PATH.is_file(), (
        f"{RATCHET_LOCKFILE_PATH.name} is missing. Section 10.4's coverage-ratchet lane "
        "installs from it and has no other dependency source - requirements.txt "
        "covers runtime only and carries no pytest, coverage or diff-cover."
    )
    return RATCHET_LOCKFILE_PATH.read_text(encoding="utf-8")


def _ratchet_lockfile_pins() -> list[str]:
    """Return the lockfile's requirement lines, without comments or blanks

    Returns:
        Every meaningful line of ``requirements-ratchet.txt``, stripped.
    """
    return [
        stripped
        for stripped in (line.strip() for line in _ratchet_lockfile_text().splitlines())
        if stripped and not stripped.startswith("#")
    ]


def test_ratchet_lockfile_pins_every_entry() -> None:
    """Pin every ratchet dependency exactly, transitive ones included"""
    unpinned = [line for line in _ratchet_lockfile_pins() if not PIN_PATTERN.match(line)]

    assert not unpinned, (
        "requirements-ratchet.txt must pin every entry with '=='. Pinning the named "
        "tools while leaving their transitive dependencies free reopens the same "
        f"hole one level down. Offending lines: {unpinned}."
    )


def test_ratchet_lockfile_has_no_local_or_vcs_reference() -> None:
    """Keep every ratchet pin resolvable from a machine that is not this one"""
    offending = [
        line
        for line in _ratchet_lockfile_pins()
        if any(marker in line for marker in UNRESOLVABLE_LOCKFILE_MARKERS)
    ]

    assert not offending, (
        "requirements-ratchet.txt contains a local path or VCS reference, which CI "
        "cannot resolve. Regenerate it with 'pip freeze --exclude-editable' and "
        f"filter the project itself out. Offending lines: {offending}."
    )


def _ratchet_lockfile_names() -> set[str]:
    """Return the canonical name of every requirement in the lockfile

    Splits on the first delimiter ``pip freeze`` can emit, so a direct-URL entry
    such as ``name @ file:///...`` is recognised as naming that project rather
    than collapsing into a single unparseable token.

    Returns:
        The canonical distribution names the lockfile mentions.
    """
    return {
        canonicalize_name(re.split(r"==|\s+@\s+|\s", line, maxsplit=1)[0])
        for line in _ratchet_lockfile_pins()
    }


def test_ratchet_lockfile_excludes_the_project_itself() -> None:
    """Leave the project out of its own lockfile"""
    project = canonicalize_name(_project_name())
    pinned = _ratchet_lockfile_names()

    assert project not in pinned, (
        f"requirements-ratchet.txt pins '{project}'. Section 10.4's lane installs the "
        "lockfile and then the project separately with 'pip install --no-deps -e .', "
        "so a self-reference here would install a wheel over the working tree the "
        "lane is supposed to measure."
    )


def test_ratchet_lockfile_contains_every_ratchet_tool() -> None:
    """Cover every tool the ratchet extra declares"""
    pinned = _ratchet_lockfile_names()
    expected = {canonicalize_name(name) for name in EXPECTED_EXTRA_BOUNDS["ratchet"]}
    missing = sorted(expected - pinned)

    assert not missing, (
        f"requirements-ratchet.txt is missing {missing}, which the 'ratchet' extra "
        "declares. It was probably regenerated from the wrong extra."
    )


def test_ratchet_lockfile_versions_satisfy_the_ratchet_extra() -> None:
    """Keep every pinned tool inside the bound the ratchet extra declares"""
    declared = {
        canonicalize_name(name): SpecifierSet(specifier)
        for name, specifier in EXPECTED_EXTRA_BOUNDS["ratchet"].items()
    }

    # The lockfile is regenerated output and the extra is its input; without this
    # they can disagree silently, which is the one drift this module exists to stop.
    violations = []
    for line in _ratchet_lockfile_pins():
        name, _, pinned = line.partition("==")
        bound = declared.get(canonicalize_name(name))
        if bound is not None and not bound.contains(Version(pinned)):
            violations.append(f"{line} violates '{bound}'")

    assert not violations, (
        "requirements-ratchet.txt pins a tool outside the bound the 'ratchet' extra "
        "declares, so CI would measure the coverage baseline with a different tool "
        f"than pyproject.toml specifies. Offending pins: {violations}."
    )


def test_ratchet_lockfile_header_records_regeneration() -> None:
    """Record the regeneration procedure in the lockfile itself"""
    header = "\n".join(
        stripped
        for stripped in (line.strip() for line in _ratchet_lockfile_text().splitlines())
        if stripped.startswith("#")
    ).lower()
    missing = [token for token in REGENERATION_HEADER_TOKENS if token not in header]

    assert not missing, (
        f"requirements-ratchet.txt's header does not record {missing}. Section 10.4 "
        "requires the file to say how it was produced, because a lockfile "
        "regenerated on the wrong platform or Python version silently moves the "
        "coverage baseline."
    )


def _design_table_constraints() -> dict[str, set[str]]:
    """Parse section 10.2's dependency table out of ``TEST_SUITE.md``

    Reads the Constraint cell of every row, which is where the policy actually
    lives. ``coverage`` yields two specifiers because the pinned lane and every
    other lane differ. The result is an unordered set, so this cannot detect the
    two ``coverage`` lanes being described the wrong way round in prose; that one
    row is checked by review, not here.

    Returns:
        A mapping of canonical tool name to the specifiers its row declares.

    Raises:
        AssertionError: If section 10.2 cannot be located.
    """
    text = TEST_SUITE_PATH.read_text(encoding="utf-8")
    assert DESIGN_TABLE_HEADING in text, (
        f"{TEST_SUITE_PATH.name} no longer contains '{DESIGN_TABLE_HEADING}'. This "
        "test reads the dependency policy from that section; a renamed heading "
        "would make the comparison vacuous rather than merely stale."
    )
    section = text.split(DESIGN_TABLE_HEADING, 1)[1].split("\n### ", 1)[0]

    constraints: dict[str, set[str]] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        # The header row and the `|---|` separator carry no backticked tool name.
        names = BACKTICKED_PATTERN.findall(cells[0]) if cells else []
        if len(cells) < 2 or not names:
            continue
        constraints[canonicalize_name(names[0])] = set(
            BACKTICKED_PATTERN.findall(cells[1])
        )

    return constraints


def _expected_specifiers_by_tool() -> dict[str, set[str]]:
    """Collapse the per-extra bounds into what section 10.2's table should declare

    Returns:
        A mapping of canonical tool name to every specifier that binds it.
    """
    collected: dict[str, set[str]] = {}
    for bounds in EXPECTED_EXTRA_BOUNDS.values():
        for name, specifier in bounds.items():
            collected.setdefault(canonicalize_name(name), set()).add(specifier)

    return collected


def _specifier_sets(entries: set[str], context: str) -> set[SpecifierSet]:
    """Convert one table cell's backticked tokens into specifier sets

    Args:
        entries: The backticked strings taken from a Constraint cell.
        context: The tool the cell belongs to, used in the failure message.

    Returns:
        One :class:`SpecifierSet` per entry, compared by meaning rather than by
        formatting so reflowing the table does not fail the check.

    Raises:
        AssertionError: If a token is not a valid version specifier.
    """
    converted = set()
    for entry in entries:
        try:
            converted.add(SpecifierSet(entry))
        except InvalidSpecifier:
            raise AssertionError(
                f"Section 10.2 of TEST_SUITE.md has a backticked token '{entry}' in "
                f"the Constraint cell for '{context}' that is not a version "
                "specifier. That cell must hold specifiers and nothing else - put "
                "prose or file names in the Purpose cell, or this guard cannot read "
                "the policy it is meant to enforce."
            ) from None

    return converted


def test_design_table_lists_every_expected_tool() -> None:
    """Parse the whole of section 10.2, so the per-tool comparison cannot go vacuous"""
    documented = set(_design_table_constraints())
    expected = set(_expected_specifiers_by_tool())

    assert documented == expected, (
        f"Section 10.2 of TEST_SUITE.md lists {sorted(documented)} but this module expects "
        f"{sorted(expected)}. Undocumented here: {sorted(documented - expected)}; "
        f"missing from the document: {sorted(expected - documented)}."
    )


@pytest.mark.parametrize("tool", sorted(_expected_specifiers_by_tool()))
def test_design_table_matches_expected_bounds(tool: str) -> None:
    """Keep section 10.2's table and the bounds asserted here in agreement"""
    documented = _specifier_sets(_design_table_constraints().get(tool, set()), tool)
    expected = _specifier_sets(_expected_specifiers_by_tool()[tool], tool)

    assert documented == expected, (
        f"Section 10.2 of TEST_SUITE.md constrains '{tool}' to "
        f"{sorted(str(entry) for entry in documented)}, but this module asserts "
        f"{sorted(str(entry) for entry in expected)} against pyproject.toml. The "
        "design document is the policy: change it and this table together."
    )
