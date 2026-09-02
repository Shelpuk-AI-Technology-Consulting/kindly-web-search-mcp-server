"""Pin the exact shape of the nodriver worker's command line.

``_build_worker_command`` in
:mod:`kindly_web_search_mcp_server.scrape.universal_html` decides which
interpreter runs the headless-browser child, which module it runs, and which
arguments it receives — including the pooled-browser arguments that let a warm
Chromium be reused instead of cold-started per request. Until it was extracted
it was a closure inside ``fetch_html_via_nodriver``, reachable only by patching
:func:`asyncio.create_subprocess_exec` and reading ``call_args``. The three
loader tests that do exactly that assert **membership** —
``assertIn("-m", args)`` — and run with the pool forced off, so the argv's
order was pinned by nothing and the pooled arguments by nothing at all.

This module owns the shape. Order is asserted by whole-list equality rather
than by membership, so a reordering that keeps every token present fails here.

**Every case passes non-default inputs, deliberately.** The shipped
``UniversalHtmlLoaderConfig.user_agent`` default is the empty string, so a case
built on the default config would expect ``--user-agent`` followed by ``""`` —
and a mutation replacing ``config.user_agent`` with a literal ``""`` would
survive the whole module. The sentinel ``EXECUTABLE`` exists for the same
reason: were the builder to read :data:`sys.executable` instead of its
parameter, an expected list built from :data:`sys.executable` could not tell.

Nothing here starts a process, opens a socket, or launches a browser. The pool
slots are plain dataclass instances; ``ChromiumSlot.ensure_started`` is
reachable only through ``pool.acquire``, which no case calls.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape import universal_html
from kindly_web_search_mcp_server.scrape.chromium_pool import ChromiumSlot
from kindly_web_search_mcp_server.scrape.universal_html import (
    UniversalHtmlLoaderConfig,
    _build_worker_command,
)

#: A path no interpreter lives at. The builder must emit what it is given, so a
#: value that could never be `sys.executable` makes "emits the parameter" and
#: "emits the running interpreter" distinguishable outcomes.
EXECUTABLE = "/nonexistent/python-under-test"

#: The module the child is expected to run. Spelled out rather than imported, so
#: renaming the worker module fails this file instead of following it silently.
WORKER_MODULE = "kindly_web_search_mcp_server.scrape.nodriver_worker"

URL = "https://example.invalid/article?ref=1"

#: Both fields non-default: the shipped user-agent default is `""`, which would
#: make a constant-`""` mutation indistinguishable from correct behaviour.
CONFIG = UniversalHtmlLoaderConfig(
    user_agent="kindly-test-agent/1.0", wait_seconds=3.5
)

#: What every case expects before any optional argument is appended.
BASE_COMMAND = [
    EXECUTABLE,
    "-m",
    WORKER_MODULE,
    "--url",
    URL,
    "--user-agent",
    "kindly-test-agent/1.0",
    "--wait-seconds",
    "3.5",
]

#: Every variable `universal_html` reads anywhere. The purity case exports all
#: of them at once; the builder must be indifferent to every one.
MODULE_ENVIRONMENT_VARIABLES = (
    "KINDLY_NODRIVER_REUSE_BROWSER",
    "KINDLY_NODRIVER_ENSURE_NO_PROXY_LOCALHOST",
    "KINDLY_HTML_TOTAL_TIMEOUT_SECONDS",
    "KINDLY_DIAGNOSTICS",
    "KINDLY_REQUEST_ID",
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "PYTHONPATH",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def _build(**overrides: Any) -> list[str]:
    """Call the builder with the minimal command's inputs, less any a case overrides

    Every parameter is named here so a case states only what it varies, and so
    a new required parameter fails this file loudly rather than one case
    obscurely.

    Args:
        **overrides: Builder keyword arguments this case replaces.

    Returns:
        The command line the builder produced.
    """
    arguments: dict[str, Any] = {
        "executable": EXECUTABLE,
        "url": URL,
        "referer": None,
        "config": CONFIG,
        "slot": None,
        "browser_executable_path": None,
    }
    arguments.update(overrides)
    return _build_worker_command(**arguments)


@pytest.fixture
def profile_directory() -> Iterator[tempfile.TemporaryDirectory[str]]:
    """Provide a real temporary directory for a pooled slot's browser profile

    A real one rather than a double: the builder reads ``.name``, and the point
    of the case is that it reads the attribute the pool actually populates.

    Yields:
        A temporary directory, removed when the case ends.
    """
    directory = tempfile.TemporaryDirectory(prefix="kindly-test-profile-")
    try:
        yield directory
    finally:
        directory.cleanup()


# --------------------------------------------------------------------------
# The minimal command
# --------------------------------------------------------------------------


def test_minimal_command_is_interpreter_module_and_three_arguments() -> None:
    """Emit the interpreter, the worker module and the three always-present flags"""
    assert _build() == BASE_COMMAND


def test_module_flag_is_immediately_followed_by_the_worker_module() -> None:
    """Keep `-m` adjacent to the module it names

    Asserted by index rather than membership: an argv holding both tokens far
    apart satisfies `assertIn` and runs the wrong thing.
    """
    command = _build()
    assert command[command.index("-m") + 1] == WORKER_MODULE


def test_wait_seconds_is_rendered_as_a_string() -> None:
    """Render the float wait time as text, since argv admits no other type

    `create_subprocess_exec` raises `TypeError: expected str, bytes or
    os.PathLike object, not float` on a bare float, so this is the difference
    between a working spawn and a crash at launch.
    """
    command = _build(
        config=UniversalHtmlLoaderConfig(user_agent="ua", wait_seconds=2.0)
    )
    assert command[command.index("--wait-seconds") + 1] == "2.0"
    assert all(isinstance(argument, str) for argument in command)


# --------------------------------------------------------------------------
# The referer
# --------------------------------------------------------------------------


def test_referer_is_appended_when_given() -> None:
    """Forward a referer as its own flag and value"""
    assert _build(referer="https://referrer.invalid/") == [
        *BASE_COMMAND,
        "--referer",
        "https://referrer.invalid/",
    ]


def test_referer_is_omitted_when_none() -> None:
    """Send no referer flag when none was supplied"""
    command = _build(referer=None)
    assert command == BASE_COMMAND
    assert "--referer" not in command


def test_referer_is_omitted_when_empty() -> None:
    """Treat an empty referer as no referer

    The shipped test is `if referer:`, not `is not None`, and `referer` is a
    public parameter of `fetch_html_via_nodriver` — so `""` is a reachable
    input. Without this case a mutation to `is not None` emits a bare
    `--referer ""` and nothing notices.
    """
    command = _build(referer="")
    assert command == BASE_COMMAND
    assert "--referer" not in command


# --------------------------------------------------------------------------
# The pooled browser
# --------------------------------------------------------------------------


def test_pooled_slot_appends_remote_host_port_and_reuse_flag() -> None:
    """Point the child at a pooled browser instead of starting its own"""
    slot = ChromiumSlot(slot_id=7, host="127.0.0.9", port=9333)
    assert _build(slot=slot) == [
        *BASE_COMMAND,
        "--remote-host",
        "127.0.0.9",
        "--remote-port",
        "9333",
        "--reuse-browser",
    ]


def test_pooled_slot_with_profile_directory_appends_user_data_dir(
    profile_directory: tempfile.TemporaryDirectory[str],
) -> None:
    """Hand the child the pooled browser's own profile directory"""
    slot = ChromiumSlot(
        slot_id=7, host="127.0.0.9", port=9333, user_data_dir=profile_directory
    )
    assert _build(slot=slot) == [
        *BASE_COMMAND,
        "--remote-host",
        "127.0.0.9",
        "--remote-port",
        "9333",
        "--reuse-browser",
        "--user-data-dir",
        profile_directory.name,
    ]


def test_pooled_slot_without_profile_directory_omits_user_data_dir() -> None:
    """Send no profile flag for a slot that owns no profile directory"""
    slot = ChromiumSlot(slot_id=7, host="127.0.0.9", port=9333, user_data_dir=None)
    assert "--user-data-dir" not in _build(slot=slot)


def test_pooled_slot_with_no_port_sends_zero() -> None:
    """Render an unassigned pooled port as zero rather than as `None`

    A slot can reach the builder before its port is known. `str(None)` would put
    the literal text `None` on the child's command line, where the worker parses
    it as an integer and fails.
    """
    slot = ChromiumSlot(slot_id=7, host="127.0.0.9", port=None)
    command = _build(slot=slot)
    assert command[command.index("--remote-port") + 1] == "0"


# --------------------------------------------------------------------------
# The browser executable
# --------------------------------------------------------------------------


def test_browser_executable_path_is_appended_after_the_pooled_arguments() -> None:
    """Place the browser path last, after anything the pool contributed

    Order, not membership: the shipped builder appends it after the pooled
    block, and a test that only checked presence would accept either.
    """
    slot = ChromiumSlot(slot_id=7, host="127.0.0.9", port=9333)
    command = _build(slot=slot, browser_executable_path="/usr/bin/chromium")
    assert command == [
        *BASE_COMMAND,
        "--remote-host",
        "127.0.0.9",
        "--remote-port",
        "9333",
        "--reuse-browser",
        "--browser-executable-path",
        "/usr/bin/chromium",
    ]


def test_browser_executable_path_is_omitted_when_absent() -> None:
    """Send no browser-path flag when the parent resolved none"""
    assert "--browser-executable-path" not in _build(browser_executable_path=None)


def test_slot_browser_executable_path_is_ignored() -> None:
    """Prefer the parent's resolved browser path over the slot's own

    `ChromiumSlot` carries a `browser_executable_path` field that the builder
    does not read, and that is correct: the parent's path is what it also
    propagates to the child through `KINDLY_BROWSER_EXECUTABLE_PATH`,
    `BROWSER_EXECUTABLE_PATH` and `CHROME_BIN`, so the two must not diverge.
    Asserted because the builder now has two visible sources for one concept and
    silently uses one.
    """
    slot = ChromiumSlot(
        slot_id=7,
        host="127.0.0.9",
        port=9333,
        browser_executable_path="/slot/never-used",
    )
    command = _build(slot=slot, browser_executable_path="/usr/bin/chromium")
    assert "/slot/never-used" not in command
    assert command[command.index("--browser-executable-path") + 1] == "/usr/bin/chromium"


# --------------------------------------------------------------------------
# Everything at once
# --------------------------------------------------------------------------


def test_every_argument_together_in_order(
    profile_directory: tempfile.TemporaryDirectory[str],
) -> None:
    """Pin the whole command line when every optional argument is present

    This is the case that fails if the referer pair moves after the pooled
    block, or the pooled block after the browser path — reorderings that every
    single-argument case above accepts.
    """
    slot = ChromiumSlot(
        slot_id=7, host="127.0.0.9", port=9333, user_data_dir=profile_directory
    )
    assert _build(
        referer="https://referrer.invalid/",
        slot=slot,
        browser_executable_path="/usr/bin/chromium",
    ) == [
        EXECUTABLE,
        "-m",
        WORKER_MODULE,
        "--url",
        URL,
        "--user-agent",
        "kindly-test-agent/1.0",
        "--wait-seconds",
        "3.5",
        "--referer",
        "https://referrer.invalid/",
        "--remote-host",
        "127.0.0.9",
        "--remote-port",
        "9333",
        "--reuse-browser",
        "--user-data-dir",
        profile_directory.name,
        "--browser-executable-path",
        "/usr/bin/chromium",
    ]


# --------------------------------------------------------------------------
# The builder's contract
# --------------------------------------------------------------------------


def test_builder_ignores_the_ambient_environment() -> None:
    """Produce the same command under an empty and a hostile environment

    The builder is the one part of this path that reads nothing ambient — the
    environment resolution happens in its callers. Asserted rather than assumed
    because two mutations in this module's history were defeated by variables
    the developer happened to export, and a battery run in one environment is
    evidence about one machine.
    """
    hostile = {name: "hostile-value" for name in MODULE_ENVIRONMENT_VARIABLES}
    with patch.dict(os.environ, {}, clear=True):
        cleared_result = _build()
    with patch.dict(os.environ, hostile, clear=True):
        hostile_result = _build()
    assert cleared_result == hostile_result == BASE_COMMAND


def test_every_parameter_is_keyword_only() -> None:
    """Admit no positional argument, so a call site cannot transpose two strings

    Five of the six parameters are strings or optionals of strings; positionally
    the url and the referer, or the two paths, are silently swappable.
    """
    parameters = inspect.signature(_build_worker_command).parameters
    assert parameters, "the builder takes no parameters at all"
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )


def test_no_public_callable_takes_a_command_parameter() -> None:
    """Keep the child command off the module's public surface

    Accepting a caller-supplied command would turn "execute an arbitrary
    process" into a supported input of a module whose url argument is already
    attacker-influenced. The seam is private by design; asserted across the
    whole public surface so the next extraction inherits the guard rather than
    writing its own.
    """
    public = {
        name: value
        for name, value in vars(universal_html).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == universal_html.__name__
    }
    # A filter that matched nothing would leave the loop below silently green.
    assert public, "the public-surface filter matched no callable"
    for name, value in public.items():
        parameters = inspect.signature(value).parameters
        assert "command" not in parameters, f"{name} takes a command parameter"
        assert "cmd" not in parameters, f"{name} takes a cmd parameter"
