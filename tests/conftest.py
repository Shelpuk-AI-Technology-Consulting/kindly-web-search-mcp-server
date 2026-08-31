from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server import settings


@pytest.fixture(scope="session", autouse=True)
def patch_settings():
    """
    Patch settings for the test session.
    """
    # Only set a dummy key if one is not provided by the environment/tests.
    # This keeps unit tests deterministic while allowing opt-in live integration tests.
    if not settings.settings.serper_api_key:
        settings.settings.serper_api_key = "test_api_key"


def pytest_addoption(parser):
    """Register the ``--min-selected`` collection floor.

    Section 10.3 of ``.system_design/TEST_SUITE.md`` requires every
    marker-selected CI job to declare how many tests it expects to select.
    ``--strict-markers`` cannot cover this: it rejects an *unknown* mark applied
    to a test, but says nothing about a job whose ``-m`` expression matches two
    tests when it should match forty.

    Defined here rather than in a repository-root ``conftest.py`` because
    ``testpaths = ["tests"]`` makes this file an initial conftest for every
    ordinary invocation. The consequence, recorded in section 10.3, is that the
    option exists only for invocations whose targets resolve under ``tests/``.

    Args:
        parser: pytest's option parser, injected by pytest.
    """
    parser.addoption(
        "--min-selected",
        type=int,
        default=0,
        help="Fail if fewer than N tests are selected.",
    )


def pytest_collection_finish(session):
    """Fail the run when fewer tests were selected than the job declared.

    ``pytest_collection_finish``, not ``pytest_collection_modifyitems``:
    ``modifyitems`` receives the full collected list *before* mark-based
    deselection, so its ``len(items)`` is the collected count and the guard would
    never fire under an ``-m`` selector. ``session.items`` here is the true
    selected count.

    Enforced inside pytest rather than by wrapping the command in shell
    arithmetic. The obvious shell form, ``pytest … || [ $? -ne 5 ]``, is broken in
    both directions: on exit 0 the ``||`` short-circuits and the under-selected
    run passes anyway, and on exit 1 it converts a genuinely failing job into a
    green one.

    Args:
        session: The collection session, injected by pytest. Its ``items`` are
            counted, never inspected.

    Raises:
        pytest.UsageError: When the floor is armed and the selection falls short.
            Raising rather than reporting is deliberate: pytest turns this into
            exit code 4, distinct from 1 (tests failed) and 5 (nothing
            collected), so a CI log distinguishes a bad selector from a bad test.
    """
    minimum = session.config.getoption("--min-selected")
    # A run that has already failed is not re-diagnosed. A module that fails to
    # import lowers the selected count too, and firing on top of that would blame
    # an innocent -m expression for an unrelated import break while replacing
    # pytest's own exit code. The guard exists to turn a *green* run red; an
    # already-red run needs nothing from it.
    if minimum and not session.testsfailed and len(session.items) < minimum:
        raise pytest.UsageError(
            f"selected {len(session.items)} tests, expected at least {minimum}; "
            "check the -m expression against the registered markers"
        )
