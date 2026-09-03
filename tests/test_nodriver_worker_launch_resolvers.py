"""Cover the nodriver worker's launch-flag and default resolution directly.

Four functions in :mod:`kindly_web_search_mcp_server.scrape.nodriver_worker`
decide how Chromium is started: whether its sandbox is enabled, which executable
is used, how many start attempts are allowed, and what the resulting command
line looks like. Those decisions used to be asserted by driving ``_fetch_html``,
the ~480-line coroutine that launches a browser — which is why adding five
required keyword-only arguments to it silently disabled every one of the
assertions at once. They are asserted here instead, against the functions that
actually make them.

None of the four is pure. ``_resolve_sandbox_enabled`` reads ``os.geteuid`` and
``KINDLY_NODRIVER_SANDBOX``; ``_resolve_browser_executable_path`` reads four
environment variables and then probes ``PATH`` through :func:`shutil.which`;
``_resolve_start_retry_attempts`` reads ``KINDLY_NODRIVER_RETRY_ATTEMPTS``; and
``_build_chromium_launch_args`` reads the two proxy variables. Every one of
those inputs is therefore pinned by the ``pinned_ambient_state`` fixture, so a
developer with ``CHROME_BIN`` exported, or a run inside a container as root,
measures the same thing as everyone else. A test that only passes on a clean
machine is not a test.

Nothing here starts a process, opens a socket or imports ``nodriver``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.scrape.nodriver_worker import (
    _build_chromium_launch_args,
    _is_snap_browser,
    _resolve_browser_executable_path,
    _resolve_sandbox_enabled,
    _resolve_start_retry_attempts,
)

#: Every environment variable the four functions under test read, in the order
#: `_resolve_browser_executable_path` consults the browser-path group. The
#: fixture below removes all of them so each case declares the whole of its own
#: input; leaving one to the ambient environment is how a suite starts passing
#: for reasons nobody chose.
READ_ENVIRONMENT_VARIABLES = (
    "KINDLY_BROWSER_EXECUTABLE_PATH",
    "BROWSER_EXECUTABLE_PATH",
    "CHROME_BIN",
    "CHROME_PATH",
    "KINDLY_NODRIVER_SANDBOX",
    "KINDLY_NODRIVER_RETRY_ATTEMPTS",
    "KINDLY_CHROME_PROXY",
    "KINDLY_CHROME_PROXY_BYPASS",
)

#: The names `_resolve_browser_executable_path` probes on `PATH`, in order.
PROBED_BROWSER_NAMES = (
    "chromium",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium-browser",
)


def _which_returning(found: dict[str, str]) -> Callable[..., str | None]:
    """Build a :func:`shutil.which` stand-in that resolves only known names.

    Args:
        found: Mapping from a command name to the absolute path it resolves to.
            Any name absent from the mapping resolves to ``None``, the way a
            real ``PATH`` lookup reports a command that is not installed.

    Returns:
        A callable with :func:`shutil.which`'s single-argument shape.
    """
    def _which(name: str, *_args: object, **_kwargs: object) -> str | None:
        return found.get(name)

    return _which


@pytest.fixture(autouse=True)
def pinned_ambient_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable these functions read and pin the process identity.

    Applied to every test in the module so a case names only the ambient state
    it varies. Three kinds of ambient input are pinned:

    ``os.geteuid`` goes to a non-root value because the sandbox resolver
    short-circuits to ``False`` for euid 0. Without the pin, every sandbox case
    would report the right answer for the wrong reason inside a container that
    runs as root, and the root case would prove nothing.

    :func:`shutil.which` goes to "nothing installed". The browser-path cases
    that set an environment variable never reach the ``PATH`` probe, so leaving
    the real :func:`shutil.which` in place would pass on any machine today —
    and would keep passing, for a different reason, on a machine with Chromium
    installed if the resolver ever consulted ``PATH`` first. A default of "no
    browser anywhere" makes the developer's own installation irrelevant to every
    case here.

    The eight environment variables go because four of them
    (``CHROME_BIN`` and friends) are commonly exported by CI images and by
    developers, and any one of them would steer
    ``_resolve_browser_executable_path`` away from what the case set up.

    Args:
        monkeypatch: pytest fixture that scopes and reverses the changes.
    """
    for name in READ_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", _which_returning({}))


# --------------------------------------------------------------------------
# _resolve_sandbox_enabled
# --------------------------------------------------------------------------


def test_sandbox_is_disabled_when_the_variable_is_unset() -> None:
    """Default the Chromium sandbox to off"""
    assert _resolve_sandbox_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "  On  "])
def test_sandbox_is_enabled_by_an_affirmative_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Enable the sandbox for every accepted affirmative spelling

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: The raw ``KINDLY_NODRIVER_SANDBOX`` value under test, including the
            case and whitespace variants the resolver normalises away.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", raw)

    assert _resolve_sandbox_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", "  no  "])
def test_sandbox_is_disabled_by_a_negative_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Disable the sandbox for every accepted negative spelling

    These cases cannot be made to fail by any mutation of the negative tuple,
    and the reason is structural rather than a weakness here: the resolver's
    fallthrough returns ``False`` too, so no input distinguishes "recognised as
    off" from "unrecognised, defaulted to off". Deleting ``"off"``, ``"no"`` or
    the whole tuple leaves every case in this module green — measured. The
    branch at that line is therefore dead code today, and a mutation report will
    say so; it is left in place because it stops being dead the day the default
    flips, and these cases start discriminating at the same moment. They are
    kept for that reason and as a record of which spellings are meant to be
    accepted.

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: The raw ``KINDLY_NODRIVER_SANDBOX`` value under test.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", raw)

    assert _resolve_sandbox_enabled() is False


@pytest.mark.parametrize("raw", ["", "   ", "maybe", "2", "enabled"])
def test_sandbox_falls_back_to_off_for_an_unrecognised_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Treat a blank or unrecognised value as the default rather than as truthy

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: A value the resolver recognises as neither affirmative nor
            negative.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", raw)

    assert _resolve_sandbox_enabled() is False


def test_sandbox_is_forced_off_when_running_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override an explicit request for the sandbox when euid is 0

    Args:
        monkeypatch: pytest fixture used to scope the identity and environment
            changes.
    """
    # Chromium generally refuses to start sandboxed as root, so the container
    # case has to win over the request rather than the other way round.
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", "1")

    assert _resolve_sandbox_enabled() is False


def test_sandbox_request_is_honoured_when_not_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the root override from swallowing an ordinary user's request

    Args:
        monkeypatch: pytest fixture used to scope the identity and environment
            changes.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 1, raising=False)
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", "1")

    assert _resolve_sandbox_enabled() is True


def test_sandbox_request_is_honoured_where_geteuid_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall through to the variable on a platform with no ``os.geteuid``

    Windows has no ``os.geteuid``. Deleting the attribute exercises that
    condition on any platform, which is the only way a Linux-only CI lane can
    cover it at all.

    This case pins the *behaviour*, not the resolver's ``hasattr`` guard, and
    deliberately so: the guard is redundant with the ``except Exception`` around
    it, since the bare call raises ``AttributeError`` where the attribute is
    absent and that is caught identically. Measured — deleting the guard from
    the source leaves all of this module's cases passing, which makes it an
    equivalent mutant rather than a hole. Anyone reading a surviving mutant
    report for that line should stop here rather than write a test for it.

    Args:
        monkeypatch: pytest fixture used to scope the attribute and environment
            changes.
    """
    monkeypatch.delattr(os, "geteuid", raising=False)
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", "1")

    assert _resolve_sandbox_enabled() is True


def test_sandbox_request_is_honoured_when_geteuid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall through to the variable when the identity lookup itself fails

    The stub raises ``RuntimeError`` rather than the ``OSError`` an identity
    lookup would plausibly produce, deliberately: the resolver catches bare
    ``Exception``, and only a non-``OSError`` case pins that breadth. It is the
    premise the neighbouring note about the ``hasattr`` guard rests on — narrow
    the catch and that guard stops being redundant — so it needs a case rather
    than a reader's goodwill.

    Args:
        monkeypatch: pytest fixture used to scope the identity and environment
            changes.
    """
    def _raise() -> int:
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(os, "geteuid", _raise, raising=False)
    monkeypatch.setenv("KINDLY_NODRIVER_SANDBOX", "1")

    assert _resolve_sandbox_enabled() is True


# --------------------------------------------------------------------------
# _resolve_browser_executable_path
# --------------------------------------------------------------------------


def test_explicit_path_wins_over_environment_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer the caller's explicit path to every other source

    Args:
        monkeypatch: pytest fixture used to scope the environment and ``PATH``
            probe changes.
    """
    monkeypatch.setenv("KINDLY_BROWSER_EXECUTABLE_PATH", "/from/env")
    monkeypatch.setattr(shutil, "which", _which_returning({"chromium": "/from/path"}))

    assert _resolve_browser_executable_path("/explicit/chrome") == "/explicit/chrome"


def test_explicit_path_is_stripped() -> None:
    """Trim surrounding whitespace from the caller's explicit path"""
    assert _resolve_browser_executable_path("  /explicit/chrome  ") == "/explicit/chrome"


@pytest.mark.parametrize("explicit", [None, "", "   "])
def test_a_blank_explicit_path_falls_through_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, explicit: str | None
) -> None:
    """Treat an absent or whitespace-only argument as no argument at all

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        explicit: The falsy explicit argument under test.
    """
    monkeypatch.setenv("KINDLY_BROWSER_EXECUTABLE_PATH", "/from/env")

    assert _resolve_browser_executable_path(explicit) == "/from/env"


@pytest.mark.parametrize(
    "key",
    [
        "KINDLY_BROWSER_EXECUTABLE_PATH",
        "BROWSER_EXECUTABLE_PATH",
        "CHROME_BIN",
        "CHROME_PATH",
    ],
)
def test_each_browser_path_variable_is_read(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Read the browser path from any of the four accepted variables

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        key: The variable under test.
    """
    monkeypatch.setenv(key, f"/from/{key}")

    assert _resolve_browser_executable_path(None) == f"/from/{key}"


def test_browser_path_variables_are_read_in_declared_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the highest-precedence variable when several are set at once

    The order is project-specific before generic: a value someone set for this
    server outranks one a CI image exported for every tool on the machine.

    Args:
        monkeypatch: pytest fixture used to scope the environment changes.
    """
    for key in (
        "KINDLY_BROWSER_EXECUTABLE_PATH",
        "BROWSER_EXECUTABLE_PATH",
        "CHROME_BIN",
        "CHROME_PATH",
    ):
        monkeypatch.setenv(key, f"/from/{key}")

    assert _resolve_browser_executable_path(None) == "/from/KINDLY_BROWSER_EXECUTABLE_PATH"


def test_a_blank_browser_path_variable_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip an exported-but-empty variable instead of returning an empty path

    Args:
        monkeypatch: pytest fixture used to scope the environment changes.
    """
    # An exported-but-empty variable is common in CI images, and returning "" as
    # the executable would fail later and further away.
    monkeypatch.setenv("KINDLY_BROWSER_EXECUTABLE_PATH", "   ")
    monkeypatch.setenv("CHROME_BIN", "/from/CHROME_BIN")

    assert _resolve_browser_executable_path(None) == "/from/CHROME_BIN"


def test_browser_path_variable_value_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trim surrounding whitespace from a variable's value

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
    """
    monkeypatch.setenv("CHROME_PATH", "  /from/CHROME_PATH  ")

    assert _resolve_browser_executable_path(None) == "/from/CHROME_PATH"


def test_browser_is_discovered_on_path_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to a ``PATH`` probe when no path is configured anywhere

    Args:
        monkeypatch: pytest fixture used to scope the ``PATH`` probe change.
    """
    monkeypatch.setattr(
        shutil, "which", _which_returning({"chromium": "/usr/bin/chromium"})
    )

    assert _resolve_browser_executable_path(None) == "/usr/bin/chromium"


@pytest.mark.parametrize("name", PROBED_BROWSER_NAMES)
def test_each_probed_browser_name_is_tried(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Probe every browser name the resolver claims to support

    Args:
        monkeypatch: pytest fixture used to scope the ``PATH`` probe change.
        name: The only command name installed for this case.
    """
    monkeypatch.setattr(shutil, "which", _which_returning({name: f"/usr/bin/{name}"}))

    assert _resolve_browser_executable_path(None) == f"/usr/bin/{name}"


def test_probed_browser_names_are_tried_in_declared_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the first installed name rather than any installed name

    Args:
        monkeypatch: pytest fixture used to scope the ``PATH`` probe change.
    """
    installed = {name: f"/usr/bin/{name}" for name in PROBED_BROWSER_NAMES}
    monkeypatch.setattr(shutil, "which", _which_returning(installed))

    assert _resolve_browser_executable_path(None) == "/usr/bin/chromium"


def test_a_configured_variable_wins_over_a_browser_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer a configured path to whatever happens to be installed

    Without this case the two lookups could be swapped without any test
    noticing: every other environment case leaves ``PATH`` empty, and the probe
    cases configure no variable, so only a case supplying both distinguishes
    the order.

    Args:
        monkeypatch: pytest fixture used to scope the environment and ``PATH``
            probe changes.
    """
    monkeypatch.setenv("CHROME_BIN", "/configured/chrome")
    monkeypatch.setattr(
        shutil, "which", _which_returning({"chromium": "/usr/bin/chromium"})
    )

    assert _resolve_browser_executable_path(None) == "/configured/chrome"


def test_no_browser_anywhere_resolves_to_none() -> None:
    """Report "not found" rather than inventing a default executable

    The caller turns this ``None`` into the error that names
    ``KINDLY_BROWSER_EXECUTABLE_PATH``; a fabricated default here would instead
    surface as an unreadable failure from Chromium itself.

    The fixture already pins :func:`shutil.which` to "nothing installed", so
    this case supplies no input at all — which is the input under test.
    """
    assert _resolve_browser_executable_path(None) is None


# --------------------------------------------------------------------------
# _resolve_start_retry_attempts
# --------------------------------------------------------------------------


def test_retry_attempts_default_to_three() -> None:
    """Allow three start attempts when the variable is unset"""
    assert _resolve_start_retry_attempts() == 3


@pytest.mark.parametrize("raw", ["1", "2", "4", "5", "  2  "])
def test_retry_attempts_accept_a_value_inside_the_range(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Honour any value the clamp leaves untouched

    The ``"  2  "`` case does not pin the resolver's ``.strip()``, and cannot:
    :func:`int` tolerates surrounding whitespace, and an all-whitespace value
    raises :class:`ValueError` into the same fallback the stripped empty string
    reaches. Removing the ``.strip()`` is an equivalent mutation — measured. The
    case is here because the resolver's *contract* accepts a padded value, which
    is worth stating whether or not one line of it is redundant.

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: The raw ``KINDLY_NODRIVER_RETRY_ATTEMPTS`` value under test.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_RETRY_ATTEMPTS", raw)

    assert _resolve_start_retry_attempts() == int(raw.strip())


@pytest.mark.parametrize("raw", ["", "   ", "three", "2.5", "0x2"])
def test_retry_attempts_fall_back_to_the_default_for_unusable_input(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Fall back to the default rather than raising on unusable input

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: A blank or unparseable value.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_RETRY_ATTEMPTS", raw)

    assert _resolve_start_retry_attempts() == 3


@pytest.mark.parametrize("raw", ["0", "-1", "-100"])
def test_retry_attempts_are_clamped_up_to_one(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Keep at least one attempt, so a zero never disables browsing entirely

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: A value at or below the floor.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_RETRY_ATTEMPTS", raw)

    assert _resolve_start_retry_attempts() == 1


@pytest.mark.parametrize("raw", ["6", "99"])
def test_retry_attempts_are_clamped_down_to_five(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Cap the attempts, so a typo cannot hold a request open indefinitely

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
        raw: A value above the ceiling.
    """
    monkeypatch.setenv("KINDLY_NODRIVER_RETRY_ATTEMPTS", raw)

    assert _resolve_start_retry_attempts() == 5


# --------------------------------------------------------------------------
# _build_chromium_launch_args
# --------------------------------------------------------------------------


def _launch_args(
    *,
    base_browser_args: list[str] | None = None,
    user_data_dir: str = "/tmp/profile",
    user_agent: str = "UA",
    host: str = "127.0.0.1",
    port: int = 9222,
    sandbox_enabled: bool = False,
) -> list[str]:
    """Build a launch-argument list from a fixed baseline.

    The parameters are spelled out rather than collected into ``**overrides``
    and splatted. A splat needs a ``dict[str, object]``, which needs a
    ``type: ignore`` on the call, which silences the type checker on exactly the
    signature drift that disabled the eight tests this module replaces. Writing
    them out costs six lines and keeps the call checkable.

    Args:
        base_browser_args: Arguments the caller supplies; defaults to none.
        user_data_dir: The isolated profile directory.
        user_agent: The user agent to advertise.
        host: The DevTools bind host.
        port: The DevTools port.
        sandbox_enabled: Whether the Chromium sandbox is enabled.

    Returns:
        The generated Chromium command-line arguments.
    """
    return _build_chromium_launch_args(
        base_browser_args=[] if base_browser_args is None else base_browser_args,
        user_data_dir=user_data_dir,
        user_agent=user_agent,
        host=host,
        port=port,
        sandbox_enabled=sandbox_enabled,
    )


def test_no_sandbox_is_passed_when_the_sandbox_is_disabled() -> None:
    """Emit ``--no-sandbox`` when the resolved decision is to disable it"""
    assert "--no-sandbox" in _launch_args(sandbox_enabled=False)


def test_no_sandbox_is_absent_when_the_sandbox_is_enabled() -> None:
    """Leave ``--no-sandbox`` off when the sandbox is enabled"""
    assert "--no-sandbox" not in _launch_args(sandbox_enabled=True)


def test_devtools_is_bound_to_the_given_loopback_host_and_port() -> None:
    """Bind the DevTools endpoint to exactly the host and port given

    The host is an argument rather than a constant because binding DevTools to
    anything but loopback would expose full browser control on the network.
    """
    args = _launch_args(host="127.0.0.1", port=54321)

    assert "--remote-debugging-host=127.0.0.1" in args
    assert "--remote-debugging-port=54321" in args


def test_the_profile_directory_is_passed_through() -> None:
    """Point Chromium at the isolated profile directory it was given"""
    assert "--user-data-dir=/tmp/kindly-profile" in _launch_args(
        user_data_dir="/tmp/kindly-profile"
    )


def test_the_user_agent_is_passed_through() -> None:
    """Pass the resolved user agent to Chromium verbatim"""
    assert "--user-agent=Mozilla/5.0 (probe)" in _launch_args(
        user_agent="Mozilla/5.0 (probe)"
    )


def test_the_unconditional_flags_are_always_present() -> None:
    """Keep every flag the builder emits regardless of its inputs

    The full constant set, not a sample. Two of these earn the assertion twice
    over: ``--headless=new`` and ``--disable-dev-shm-usage`` are what make a run
    inside a container work at all, and ``--disable-logging`` is the flag the
    de-duplication case below uses as its probe — a premise that was unpinned
    until this case named it.
    """
    args = _launch_args()

    for flag in (
        "--headless=new",
        "--window-size=1920,1080",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--disable-logging",
        "--log-level=3",
    ):
        assert flag in args, f"{flag} is no longer emitted unconditionally"


def test_no_proxy_flags_are_emitted_when_the_proxy_is_not_configured() -> None:
    """Leave the proxy flags off entirely when no proxy is configured"""
    args = _launch_args()

    assert not any(arg.startswith("--proxy-server=") for arg in args)
    assert not any(arg.startswith("--proxy-bypass-list=") for arg in args)


def test_the_proxy_bypass_list_is_emitted_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass the configured bypass list on to Chromium

    Args:
        monkeypatch: pytest fixture used to scope the environment change.
    """
    monkeypatch.setenv("KINDLY_CHROME_PROXY_BYPASS", "localhost,127.0.0.1")

    assert "--proxy-bypass-list=localhost,127.0.0.1" in _launch_args()


def test_base_arguments_are_appended_after_the_built_ones() -> None:
    """Append the caller's arguments last, so they can override what precedes"""
    args = _launch_args(base_browser_args=["--custom-flag"])

    assert args[-1] == "--custom-flag"


def test_a_base_argument_already_present_is_not_repeated() -> None:
    """Drop a duplicate rather than passing the same flag to Chromium twice"""
    args = _launch_args(base_browser_args=["--disable-logging", "--custom-flag"])

    assert args.count("--disable-logging") == 1
    assert args[-1] == "--custom-flag"


# --------------------------------------------------------------------------
# _is_snap_browser
# --------------------------------------------------------------------------

#: The launcher a stock Ubuntu install puts on ``PATH``. It is a **symlink to**
#: ``/usr/bin/snap`` -- the snap runtime, not the browser -- which is what makes
#: this the case worth writing: resolving the path first replaces the only
#: evidence that the browser is snap-packaged with a target that carries no
#: marker at all. Measured on Ubuntu 24.04.4:
#: ``lrwxrwxrwx /snap/bin/chromium -> /usr/bin/snap``.
UBUNTU_SNAP_LAUNCHER = "/snap/bin/chromium"

#: What :func:`os.path.realpath` returns for :data:`UBUNTU_SNAP_LAUNCHER`. Pinned
#: rather than derived so the case answers the same on a machine with no snap
#: installed, and on Windows, where ``realpath`` gives a third answer again.
UBUNTU_SNAP_LAUNCHER_TARGET = "/usr/bin/snap"

#: An ordinary system browser: no marker in the path, and it resolves to itself.
SYSTEM_BROWSER = "/usr/bin/chromium"

#: The same launcher on a distribution that is **not** Ubuntu. snapd mounts snaps
#: under ``/var/lib/snapd/snap`` and puts ``/var/lib/snapd/snap/bin`` on ``PATH``;
#: ``/snap`` exists there only if an administrator creates the symlink by hand,
#: which classic confinement requires and nothing else does (ArchWiki, *Snap*).
#:
#: It is here because it is the only shape that distinguishes the marker rule the
#: detector implements -- ``"/snap/" in path`` -- from the ``startswith("/snap/")``
#: the shipped function used and that a later reader will be tempted to restore.
#: Every other path in this module carries the marker at position 0, so without
#: this constant that mutation survives the whole suite while quietly denying the
#: snap allowance to every snap browser on Fedora, Arch and Debian.
SNAPD_MOUNT_LAUNCHER = "/var/lib/snapd/snap/bin/chromium"


def _realpath_returning(targets: dict[str, str]) -> Callable[..., str]:
    """Build an :func:`os.path.realpath` stand-in with a fixed answer table.

    Args:
        targets: Mapping from a path to what ``realpath`` should return for it.
            A path absent from the mapping resolves to itself, which is what the
            real call does for a path that is not a symlink.

    Returns:
        A callable with :func:`os.path.realpath`'s single-argument shape.
    """
    def _realpath(path: str, *_args: object, **_kwargs: object) -> str:
        return targets.get(path, path)

    return _realpath


@pytest.fixture
def posix_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``os.name`` to ``"posix"`` for the duration of one case.

    `_is_snap_browser` has exactly two ambient inputs, and this is the second
    one: snap is a Linux packaging format, so the function answers ``False`` for
    every path where ``os.name`` is not ``"posix"``. A case that leaves it to the
    host asserts one thing on Linux and its opposite on Windows -- which is the
    failure the cross-platform milestone actually hit, in a case that derived a
    snap classification from a path shape and expected a constant.

    On Linux this pin is a no-op and is applied anyway, because a pin that only
    exists where it changes nothing is a pin nobody notices has gone missing.

    Args:
        monkeypatch: pytest fixture that scopes and reverses the change.
    """
    monkeypatch.setattr(os, "name", "posix")


def test_the_ubuntu_snap_launcher_is_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify ``/snap/bin/chromium`` as snap even though it resolves elsewhere

    This is the regression case. The shipped function resolved the path before
    testing it, so the single commonest way to have a snap Chromium -- the only
    way Ubuntu has offered since 19.10 -- classified as **non**-snap and was
    denied the longer DevTools budget the multiplier exists to give it.

    A browser invoked as ``/snap/bin/chromium`` is a snap browser whatever the
    launcher symlink points at, so the marker is tested on the path as given.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to pin ``os.path.realpath``.
    """
    monkeypatch.setattr(
        os.path,
        "realpath",
        _realpath_returning({UBUNTU_SNAP_LAUNCHER: UBUNTU_SNAP_LAUNCHER_TARGET}),
    )

    assert _is_snap_browser(UBUNTU_SNAP_LAUNCHER) is True


def test_a_snap_path_with_nothing_to_resolve_is_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify a ``/snap/`` path that is not a symlink as snap

    The shape the shipped function got right, kept under test because it is the
    shape every existing test in the tree uses to reach the snap branch: a path
    under ``/snap/`` that does not exist has no symlink for ``realpath`` to
    follow, so both the given and the resolved form carry the marker.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to pin ``os.path.realpath``.
    """
    monkeypatch.setattr(os.path, "realpath", _realpath_returning({}))

    assert _is_snap_browser("/snap/bin/chromium-for-tests") is True


def test_a_launcher_under_the_snapd_mount_point_is_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify the same launcher as snap where snapd mounts outside ``/snap``

    The case above is the Ubuntu shape, where the marker happens to sit at the
    start of the path. Off Ubuntu it does not: snapd mounts under
    ``/var/lib/snapd/snap`` and ``/snap`` is a hand-made symlink that only
    classic confinement needs. The launcher is a symlink to ``/usr/bin/snap``
    there too, so the same defect applies -- and the same repair, only if the
    marker is matched anywhere in the path rather than at its start.

    Without this case ``"/snap/" in executable_path`` can be narrowed to
    ``executable_path.startswith("/snap/")`` -- the spelling the shipped function
    used -- with the whole suite still green, reintroducing this bug for every
    snap browser on Fedora, Arch and Debian. Measured.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to pin ``os.path.realpath``.
    """
    monkeypatch.setattr(
        os.path,
        "realpath",
        _realpath_returning({SNAPD_MOUNT_LAUNCHER: UBUNTU_SNAP_LAUNCHER_TARGET}),
    )

    assert _is_snap_browser(SNAPD_MOUNT_LAUNCHER) is True


def test_a_browser_resolving_into_the_snap_tree_is_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify a marker-free path that resolves into ``/snap/`` as snap

    Resolving is still done, and this is why: a browser reached by an ordinary
    path can be a snap package underneath, and only the resolved form says so.
    Deleting the resolved-path test to keep the given-path one would fix the
    defect above and open this hole in its place, so both halves are asserted.

    The target deliberately carries the marker **mid-path**, under snapd's own
    mount point rather than under ``/snap``. It is the more representative real
    answer off Ubuntu, and it is what stops ``"/snap/" in resolved`` being
    narrowed back to ``resolved.startswith("/snap/")`` -- the spelling the shipped
    function used, which survives every prefix-shaped case. See
    :data:`SNAPD_MOUNT_LAUNCHER`.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to pin ``os.path.realpath``.
    """
    monkeypatch.setattr(
        os.path,
        "realpath",
        _realpath_returning(
            {SYSTEM_BROWSER: "/var/lib/snapd/snap/chromium/2917/usr/lib/chromium/chrome"}
        ),
    )

    assert _is_snap_browser(SYSTEM_BROWSER) is True


def test_an_ordinary_system_browser_is_not_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave a distribution-packaged browser unclassified, so it keeps the plain budget

    The negative polarity of the classification. Without it, a function that
    returned ``True`` unconditionally would satisfy every case above, and the
    snap multiplier would silently apply to every browser on the machine.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to pin ``os.path.realpath``.
    """
    monkeypatch.setattr(os.path, "realpath", _realpath_returning({}))

    assert _is_snap_browser(SYSTEM_BROWSER) is False


def test_no_path_is_classified_as_snap_away_from_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""Answer ``False`` for every path where ``os.name`` is not ``"posix"``

    Snap is a Linux packaging format and no Windows machine has one, so this is
    the correct answer rather than a limitation. It used to fall out of an
    accident: a path with a single leading slash is not absolute on Windows, so
    ``realpath`` joins it against the current drive and normalises the
    separators -- ``/snap/bin/chromium`` becomes ``D:\snap\bin\chromium`` and the
    marker is erased. Measured against the shipped function on a
    ``windows-latest`` runner, where the drive happened to be ``D:``.

    Testing the path **as given** is what removes that accident, so the answer
    now rests on an explicit guard instead. The realpath pin below reproduces the
    measured Windows answer, so the case fails for the right reason when the
    guard is deleted rather than passing for the old one.

    **One equivalent mutant, triaged here rather than chased.** Rewriting the
    guard as ``if os.name == "nt": return False`` survives this case and the whole
    suite. It is equivalent on any interpreter that can run this package:
    CPython sets ``os.name`` to ``"posix"`` or ``"nt"`` and to nothing else, so
    the two spellings partition the same inputs. Measured. No test should be
    written for it.

    Args:
        monkeypatch: pytest fixture used to pin ``os.name`` and
            ``os.path.realpath``.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        os.path,
        "realpath",
        _realpath_returning({UBUNTU_SNAP_LAUNCHER: r"D:\snap\bin\chromium"}),
    )

    assert _is_snap_browser(UBUNTU_SNAP_LAUNCHER) is False


def test_a_snap_path_is_still_classified_as_snap_when_it_cannot_be_resolved(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answer from the path alone when ``realpath`` fails on a ``/snap/`` path

    The ordering case. The marker is tested on the path as given **before** the
    resolver is called, and this is the only case that can tell that ordering
    from the tidier-looking arrangement a future reader will reach for:

    .. code-block:: python

        try:
            resolved = os.path.realpath(executable_path)
        except Exception:
            return False
        return "/snap/" in executable_path or "/snap/" in resolved

    That version satisfies every other case in this module and at both call
    sites, and answers ``False`` here -- where the shipped function and the
    repaired one both answer ``True``. It is a live, non-equivalent mutation of
    the one property this repair exists to establish, so it gets a case rather
    than a comment.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to make ``os.path.realpath`` fail.
    """
    def _raising(_path: str, *_args: object, **_kwargs: object) -> str:
        raise OSError("filesystem is unavailable")

    monkeypatch.setattr(os.path, "realpath", _raising)

    assert _is_snap_browser(UBUNTU_SNAP_LAUNCHER) is True


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError("filesystem is unavailable"), id="filesystem-error"),
        pytest.param(
            ValueError("embedded null character in path"), id="embedded-null"
        ),
    ],
)
def test_a_path_that_cannot_be_resolved_is_not_classified_as_snap(
    posix_platform: None,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Report a browser whose path will not resolve as non-snap rather than raising

    ``realpath`` can fail two ways, and both are driven here rather than named
    and left: a filesystem error raises :class:`OSError`, and an embedded null
    raises :class:`ValueError` (measured --
    ``os.path.realpath("/usr/bin/chromium\x00")`` reports *lstat: embedded null
    character in path*). A classification is not worth aborting a browser launch
    over, so either failure answers ``False``.

    Driving both is what keeps the ``except Exception:`` clause honest. Narrowing
    it to ``except OSError:`` -- which this repository's own ``ruff`` run
    actively suggests, reporting ``BLE001`` on that very line -- survives an
    ``OSError``-only case and turns the null path into an uncaught raise out of a
    browser launch. Neither failure is reachable from production input today
    (the path comes from the environment or from `shutil.which`, and neither can
    carry a null), which is why this is a parametrized pair rather than a wider
    harness.

    The input carries no marker of its own, deliberately: a ``/snap/`` path never
    reaches the resolver at all, so a case using one would pass whatever the
    ``except`` branch did and could not tell ``return False`` from ``return
    True``. The case above pins that ordering separately.

    Args:
        posix_platform: fixture pinning ``os.name``.
        monkeypatch: pytest fixture used to make ``os.path.realpath`` fail.
        failure: The exception ``os.path.realpath`` raises for this row.
    """
    def _raising(_path: str, *_args: object, **_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr(os.path, "realpath", _raising)

    assert _is_snap_browser(SYSTEM_BROWSER) is False
