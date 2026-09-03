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

import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch

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

    The detector's own two ambient inputs are **not** pinned here: they are
    pinned around its single call, by :func:`_pinned_detector` below, so that
    nothing else in the slot's startup sees a rewritten ``os.name`` or
    ``os.path.realpath``.

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
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)


def _pinned_detector(realpath_targets: dict[str, str]) -> Any:
    """Build a `_is_snap_browser` double that runs the **real** one under pinned inputs.

    The classification these cases assert has to be *derived* from the path --
    that is the wiring they exist to prove -- but both of the detector's ambient
    inputs answer differently per machine and per platform: ``os.path.realpath``
    depends on where the developer's Chromium came from, and ``os.name`` decides
    whether anything may classify as snap at all. Pinning either for the whole
    slot start would be a process-global reaching well past the subject, and the
    real :class:`tempfile.TemporaryDirectory` runs inside that window. So the
    pins are scoped to the detector's own call.

    Autospecced like the other doubles here, so a production change calling the
    detector with the wrong arity raises rather than being quietly accepted.

    **`tests/test_nodriver_worker_sandbox.py::make_pinned_detector` is the same
    nine lines**, for the other call site. Deliberately not shared -- see that
    one's docstring for why. If you change which inputs are pinned, change both.

    Args:
        realpath_targets: What ``os.path.realpath`` should answer, by path. A
            path absent from the mapping resolves to itself, which is what the
            real call does for an executable that is not a symlink.

    Returns:
        An autospec double of `_is_snap_browser` returning the real answer.
    """
    real_detector = worker._is_snap_browser

    def _realpath(path: str, *_args: object, **_kwargs: object) -> str:
        return realpath_targets.get(path, path)

    def _classify(executable_path: str) -> bool:
        with (
            patch.object(worker.os, "name", "posix"),
            patch.object(worker.os.path, "realpath", _realpath),
        ):
            return real_detector(executable_path)

    return create_autospec(worker._is_snap_browser, side_effect=_classify)


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

    Four seams are installed, and they are what keeps this a unit test: the port
    picker would bind a socket, `_launch_chromium` would spawn a real Chromium,
    `_wait_for_devtools_ready` would speak HTTP to it, and the detector's two
    ambient inputs would otherwise come from the host. The profile directory is
    left real and removed again below.

    Args:
        monkeypatch: pytest fixture used to set the executable variable.
        executable_path: The browser path the slot should start.
        realpath_targets: What ``os.path.realpath`` answers inside the detector,
            by path.

    Returns:
        The ``timeout_seconds`` `_wait_for_devtools_ready` was called with.
    """
    monkeypatch.setenv("KINDLY_BROWSER_EXECUTABLE_PATH", executable_path)

    slot = chromium_pool.ChromiumSlot(slot_id=0)
    detector = _pinned_detector(realpath_targets)
    with (
        patch.object(worker, "_is_snap_browser", detector),
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

    # Asked once, and about the path the slot resolved -- not about some default
    # reached for elsewhere.
    detector.assert_called_once_with(executable_path)
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

    The classification is **derived from the path**, not injected: both of the
    detector's ambient inputs are pinned around its own call, so the case
    asserts one constant everywhere without rewriting them for the rest of the
    slot's startup.

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
