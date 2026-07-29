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

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"

# The MCP SDK removed ``mcp.server.fastmcp`` in 2.0.0. The later versions are
# listed deliberately: a too-narrow pin such as ``!=2.0.0`` excludes exactly the
# one release while leaving 2.1.0 and 3.0.0 installable, which would reintroduce
# the outage while still satisfying a check that only tried 2.0.0.
REJECTED_MCP_VERSIONS = ("2.0.0", "2.1.0", "3.0.0")

# Matches the lower bound declared in ``pyproject.toml``. Raising that floor
# should update this too; the pairing is what makes an accidental
# over-tightening visible instead of silent.
SUPPORTED_MCP_VERSION = "1.25.0"


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
