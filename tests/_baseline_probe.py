"""Child-process pytest plugin recording one run's outcomes as JSON.

Loaded with ``-p`` by :mod:`tests.test_baseline_failure_ledger` into the child
process it spawns, and by nothing else. It exists because the human-readable
short summary is not a usable channel for this: measured on pytest 9.1.1, a
failing ``unittest`` subtest prints a **passing** dot in the progress line and a
``SUBFAILED(i=2) <nodeid>`` line in the summary, never the word ``FAILED``. A
guard reading ``FAILED`` lines therefore sees an empty failure set on a red
suite -- and two of the three ``subTest`` sites in this repository sit in
``tests/test_universal_html_loader.py``, one of the files the ledger tracks.

Reporting the collected and skipped sets alongside the failing one is what lets
the guard tell a repaired test from a deleted, renamed or newly skipped one.
Those look identical in a failure set -- the id simply stops appearing -- and
only one of them is good news.

The module is deliberately not named ``test_*``, so pytest does not collect it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_collected: set[str] = set()
_failed: set[str] = set()
_skipped: set[str] = set()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the option naming the file this plugin writes.

    An option rather than an environment variable: the guard scrubs whole
    environment prefixes before spawning, and a channel that a scrubbing rule
    could silently remove is the wrong channel. As argv it is visible in any
    failure message that quotes the child's command line.

    Args:
        parser: pytest's option parser, injected by pytest.
    """
    parser.addoption(
        "--baseline-probe-json",
        default=None,
        help="Write this run's collected/failed/skipped node ids to this path.",
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Record every node id that survived selection.

    ``pytest_collection_finish``, not ``pytest_collection_modifyitems``, for the
    reason ``tests/conftest.py`` already records about its own hook: ``modifyitems``
    receives the collected list *before* mark-based deselection. Measured on
    pytest 9.1.1 with ``-m "not slow"`` over two tests, ``modifyitems`` sees two
    items and ``session.items`` sees one.

    It matters for what a deselected test is called. The child passes no ``-m``
    today, but the ledger's own measurement command gains marker selection in a
    later workstream. Under ``modifyitems``, a documented test that was
    *deselected* would appear as collected, not failing and not skipped -- which
    is the guard's signature for "ran and passed, delete the id". That is the one
    diagnosis the ledger exists to prevent.

    Args:
        session: The collection session, injected by pytest. Its ``items`` are the
            tests that will actually run.
    """
    _collected.update(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record the outcome of one test phase.

    Every phase is inspected, not only ``call``: a fixture that raises reports a
    ``failed`` setup phase, which pytest prints as ``ERROR`` and which is just as
    much a red test as an assertion failure. Subtest reports arrive through this
    same hook carrying the parent test's node id, which is the whole reason the
    plugin exists.

    Args:
        report: The phase report, injected by pytest.
    """
    if report.failed:
        _failed.add(report.nodeid)
    elif report.skipped:
        _skipped.add(report.nodeid)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write the recorded sets to the path the option named.

    Args:
        session: The finished session, injected by pytest.
    """
    destination = session.config.getoption("--baseline-probe-json")
    if destination is None:
        return
    # Sorted so the file is stable to diff by hand when a guard failure needs
    # investigating.
    Path(destination).write_text(
        json.dumps(
            {
                "collected": sorted(_collected),
                "failed": sorted(_failed),
                "skipped": sorted(_skipped),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
