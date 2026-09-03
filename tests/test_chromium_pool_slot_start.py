"""Cover the DevTools budget a pooled Chromium slot starts its browser with.

`ChromiumSlot._start` is the browser pool's counterpart to `_fetch_html`: it
resolves an executable, picks a port, launches Chromium and waits for its
DevTools endpoint. It shares one decision with `_fetch_html` and owns its own
copy of it -- a snap-packaged browser is slow to open that endpoint, so its
timeout is multiplied. The two copies can drift, and a defect in the shared
detector reached both of them, so the budget is asserted at each call site
rather than at one and inferred at the other.

Only the budget is asserted here. Port selection, profile directories, the slot
health probe and the pool's queueing belong to whoever tests the pool as a
whole; this module exists because the snap allowance had no test at this call
site at all.

Nothing here starts a browser, opens a socket or reaches the network: the three
collaborators that would (`_launch_chromium`, `_wait_for_devtools_ready` and the
port picker) are replaced with autospec doubles, so a call with the wrong arity
raises inside the code under test instead of being silently recorded. The real
:class:`tempfile.TemporaryDirectory` still runs and is removed again by the
helper below.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape import chromium_pool
from kindly_web_search_mcp_server.scrape import nodriver_worker as worker

#: Every environment variable `ChromiumSlot._start` reads, directly or through
#: the worker helpers it calls. Removed before each case so a case declares the
#: whole of its own input: four of them are browser paths that CI images and
#: developers commonly export, and any one would steer the executable resolver
#: away from the path the case is about.
READ_ENVIRONMENT_VARIABLES = (
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "KINDLY_NODRIVER_SANDBOX",
    "KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS",
    "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER",
    "KINDLY_CHROME_PROXY",
    "KINDLY_CHROME_PROXY_BYPASS",
)

#: The base DevTools budget these cases configure, and the multiplier a snap
#: browser earns. Two distinct values, so a production change that read the
#: multiplier where it meant the base -- or the reverse -- cannot land on the
#: expected number by coincidence.
DEVTOOLS_READY_TIMEOUT_SECONDS = "4"
SNAP_BACKOFF_MULTIPLIER = "3"

#: The launcher a stock Ubuntu install provides. It is a symlink to
#: ``/usr/bin/snap`` -- the snap runtime, not a browser -- which is why resolving
#: it before testing for the ``/snap/`` marker used to classify the commonest
#: snap Chromium as an ordinary one. Measured on Ubuntu 24.04.4.
SNAP_LAUNCHER_PATH = "/snap/bin/chromium"

#: What ``os.path.realpath`` returns for :data:`SNAP_LAUNCHER_PATH`, pinned
#: rather than looked up so the case answers the same on a machine with no snap
#: installed.
SNAP_LAUNCHER_TARGET = "/usr/bin/snap"

#: An ordinary distribution-packaged browser: no marker, and it resolves to
#: itself.
SYSTEM_BROWSER_PATH = "/usr/bin/chromium"


@pytest.fixture(autouse=True)
def pinned_ambient_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every ambient input these cases do not vary.

    ``os.name`` is pinned because `_is_snap_browser` refuses to classify
    anything as snap away from POSIX -- snap is a Linux packaging format -- so a
    case that left it to the host would assert one budget on Linux and the other
    on Windows. That is not hypothetical: a case elsewhere in this suite did
    exactly that and went red on the first Windows run this repository ever
    took. On Linux the pin changes nothing and is applied anyway.

    :func:`shutil.which` is pinned to "nothing installed" although both cases
    set an explicit executable variable and never reach the ``PATH`` probe. A
    case that leaves the real lookup in place is one whose result depends on
    whether the developer has Chromium installed, and it would start depending
    on it the day the resolver consults ``PATH`` first.

    Args:
        monkeypatch: pytest fixture that scopes and reverses the changes.
    """
    for name in READ_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "KINDLY_NODRIVER_DEVTOOLS_READY_TIMEOUT_SECONDS", DEVTOOLS_READY_TIMEOUT_SECONDS
    )
    # Configured in both cases, deliberately: the non-snap case must leave the
    # budget alone because the browser is not snap, not because no multiplier
    # was set.
    monkeypatch.setenv(
        "KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER", SNAP_BACKOFF_MULTIPLIER
    )
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)


async def _devtools_budget_for(
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable_path: str,
    realpath_targets: dict[str, str],
) -> float:
    """Start one pooled slot and report the DevTools budget it waited with.

    The executable is supplied through ``KINDLY_BROWSER_EXECUTABLE_PATH`` rather
    than by doubling the resolver, so the real resolution chain runs and the
    case's subject stays the classification rather than a stand-in for it.

    Args:
        monkeypatch: pytest fixture used to set the executable variable and pin
            ``os.path.realpath``.
        executable_path: The browser path the slot should start.
        realpath_targets: What ``os.path.realpath`` answers, by path. A path
            absent from the mapping resolves to itself, which is what the real
            call does for an executable that is not a symlink.

    Returns:
        The ``timeout_seconds`` `_wait_for_devtools_ready` was called with.
    """
    monkeypatch.setenv("KINDLY_BROWSER_EXECUTABLE_PATH", executable_path)

    def _realpath(path: str, *_args: object, **_kwargs: object) -> str:
        return realpath_targets.get(path, path)

    monkeypatch.setattr(os.path, "realpath", _realpath)

    slot = chromium_pool.ChromiumSlot(slot_id=0)
    with (
        patch.object(worker, "_launch_chromium", autospec=True),
        patch.object(worker, "_wait_for_devtools_ready", autospec=True) as wait_ready,
        patch.object(chromium_pool, "_pick_port", autospec=True, return_value=9222),
    ):
        try:
            await slot._start(user_agent="kindly-test-agent", port_range=None, diagnostics=None)
        finally:
            # `_start` creates a real profile directory; the slot's own teardown
            # would also kill a process that was never launched here.
            if slot.user_data_dir is not None:
                slot.user_data_dir.cleanup()
                slot.user_data_dir = None

    return float(wait_ready.call_args.kwargs["timeout_seconds"])


async def test_the_snap_launcher_path_lengthens_the_pooled_devtools_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiply the pooled DevTools budget for the Ubuntu snap launcher

    A snap Chromium is the browser that most needs the longer budget, and until
    the detector was repaired it was the one browser certain never to get it:
    ``/snap/bin/chromium`` is a symlink to ``/usr/bin/snap``, and resolving it
    before testing for the marker threw away the evidence. On the shipped
    defaults that is 12 s where 36 s was intended.

    The classification is **derived from the path**, not injected. Both of its
    ambient inputs are pinned -- ``os.name`` by the fixture and
    ``os.path.realpath`` here -- so the case asserts one constant everywhere.

    Args:
        monkeypatch: pytest fixture used to pin this case's inputs.
    """
    budget = await _devtools_budget_for(
        monkeypatch,
        executable_path=SNAP_LAUNCHER_PATH,
        realpath_targets={SNAP_LAUNCHER_PATH: SNAP_LAUNCHER_TARGET},
    )

    assert budget == 12.0


async def test_a_system_browser_path_leaves_the_pooled_devtools_budget_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave the pooled DevTools budget unmultiplied for a system browser

    The negative polarity, and it is load-bearing rather than symmetric: a
    ``_start`` that multiplied unconditionally would satisfy the case above on
    its own. Nothing in this repository asserted this branch before -- the pool
    had no test module -- so the mutation that deletes its ``if`` was live.

    Args:
        monkeypatch: pytest fixture used to pin this case's inputs.
    """
    budget = await _devtools_budget_for(
        monkeypatch,
        executable_path=SYSTEM_BROWSER_PATH,
        realpath_targets={},
    )

    assert budget == 4.0
