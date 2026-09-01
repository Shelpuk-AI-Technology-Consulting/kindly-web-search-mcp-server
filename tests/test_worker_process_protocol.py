"""Guard the ``WorkerProcess`` Protocol and the typed double built against it.

Section 8A step 3 of ``.system_design/TEST_SUITE.md`` is the policy;
:mod:`kindly_web_search_mcp_server.scrape.types` is what production declares and
:mod:`tests.doubles.worker_process` is what the tests hand to it. This module
keeps the two in step.

The Protocol exists because an implicit agreement between the spawn site and its
test doubles already failed once: production stopped calling ``communicate()``
and started reading ``proc.stdout``, and the double in
``tests/test_universal_html_loader.py`` was never updated.

**Three mechanisms are needed, because none of them is sufficient alone.**
Measured on CPython 3.13 and mypy 2.3.1:

===============================================  ================  =========================
Check                                            Missing attribute  Wrong ``wait()`` signature
===============================================  ================  =========================
``isinstance`` without ``@runtime_checkable``    ``TypeError``     ``TypeError``
``isinstance`` with ``@runtime_checkable``       caught            **not caught**
static check                                     caught            caught
===============================================  ================  =========================

The bottom-right cell is why the child-process type-check cases below exist at
all: a signature gaining arguments is exactly the drift a runtime ``isinstance``
answers ``True`` to.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kindly_web_search_mcp_server.scrape.types import WorkerProcess
from tests.doubles.worker_process import FakeWorkerProcess, primed_reader

# The exact surface `fetch_html_via_nodriver` reads off the child process. Kept
# as a literal so padding the Protocol with a member production never consumes
# fails here rather than passing unnoticed.
CONSUMED_SURFACE: Final = frozenset(
    {"stdout", "stderr", "pid", "returncode", "wait", "kill", "terminate"}
)


def test_protocol_declares_exactly_the_consumed_surface() -> None:
    """Name every member production reads, and no member it does not"""
    declared = frozenset(WorkerProcess.__protocol_attrs__)

    assert declared == CONSUMED_SURFACE, (
        "WorkerProcess and the surface production consumes disagree. Extra: "
        f"{sorted(declared - CONSUMED_SURFACE)}; missing: "
        f"{sorted(CONSUMED_SURFACE - declared)}."
    )


def test_protocol_is_runtime_checkable() -> None:
    """Answer isinstance rather than raising, so the presence check is usable"""
    # Asserted behaviourally rather than by reading `_is_runtime_protocol`: the
    # private attribute is a CPython implementation detail, while the property
    # that matters is that the call answers instead of raising.
    try:
        isinstance(object(), WorkerProcess)
    except TypeError as error:  # pragma: no cover - only on a broken Protocol
        raise AssertionError(
            "WorkerProcess is not @runtime_checkable, so isinstance() against "
            f"it raises instead of answering: {error}"
        ) from error


# --------------------------------------------------------------------------
# The typed double
# --------------------------------------------------------------------------


def test_fake_declares_every_protocol_member() -> None:
    """Expose every member production reads, so a gap fails here not at runtime"""
    fake = FakeWorkerProcess()
    missing = sorted(name for name in CONSUMED_SURFACE if not hasattr(fake, name))

    assert not missing, (
        f"FakeWorkerProcess is missing {missing}. This is the original outage: "
        "the previous double declared `communicate()` while production read "
        "`proc.stdout`, and nothing caught it until three tests raised "
        "AttributeError."
    )


def test_fake_methods_are_callable() -> None:
    """Make each method callable, which presence alone does not guarantee"""
    fake = FakeWorkerProcess()
    not_callable = sorted(
        name for name in ("wait", "kill", "terminate") if not callable(getattr(fake, name))
    )

    assert not not_callable, (
        f"FakeWorkerProcess exposes {not_callable} as data rather than as "
        "methods. `runtime_checkable` verifies presence only, so nothing else "
        "catches this."
    )


async def test_fake_wait_returns_a_coroutine() -> None:
    """Keep `wait()` awaitable, since production awaits it"""
    fake = FakeWorkerProcess(exit_code=7)
    pending = fake.wait()
    is_coroutine = asyncio.iscoroutine(pending)
    # Closed before asserting: a failed assertion would otherwise leave the
    # coroutine un-awaited and add a RuntimeWarning on top of the real failure.
    if not is_coroutine:
        assert is_coroutine, (
            "FakeWorkerProcess.wait() did not return a coroutine, so "
            "production's `await proc.wait()` would fail at runtime."
        )
    assert await pending == 7


async def test_fake_streams_yield_bytes() -> None:
    """Round-trip real output through the same reader production reads from.

    This case does **not** prove the ``strict_bytes`` constraint, and an earlier
    draft that asserted ``type(stdout) is bytes`` only appeared to. Measured:
    :meth:`asyncio.StreamReader.read` returns ``bytes`` whether it was fed
    ``bytes``, ``bytearray`` or ``memoryview``, so that assertion could not fail.
    The constraint binds at the call site instead, and is enforced statically by
    ``tests/typing_negative/bytearray_payload.py``.
    """
    fake = FakeWorkerProcess(
        stdout=primed_reader(b"<html>ok</html>"), stderr=primed_reader(b"noise")
    )
    assert fake.stdout is not None and fake.stderr is not None

    assert await fake.stdout.read() == b"<html>ok</html>"
    assert await fake.stderr.read() == b"noise"


async def test_fake_returncode_is_none_until_the_process_exits() -> None:
    """Model the exit transition, because production loops on `returncode is None`"""
    fake = FakeWorkerProcess(exit_code=3)

    assert fake.returncode is None, (
        "FakeWorkerProcess reported an exit status before the process "
        "completed. Production's heartbeat is spelled `while proc.returncode is "
        "None`, so a double born already exited cannot exercise it at all."
    )
    await fake.wait()
    assert fake.returncode == 3


# --------------------------------------------------------------------------
# What `isinstance` can and cannot see
# --------------------------------------------------------------------------


class _DoubleMissingStdout:
    """A double lacking `stdout`, which the runtime check does catch."""

    stderr = None
    pid = 1
    returncode = None

    async def wait(self) -> int:
        """Return an exit code."""
        return 0

    def kill(self) -> None:
        """Kill the process."""

    def terminate(self) -> None:
        """Terminate the process."""


class _DoubleWithWrongWaitSignature:
    """A double whose `wait()` takes an argument production never passes."""

    stdout = None
    stderr = None
    pid = 1
    returncode = None

    async def wait(self, timeout: float) -> int:
        """Return an exit code, after taking an argument nothing supplies.

        Args:
            timeout: An argument production does not pass.

        Returns:
            The exit code.
        """
        return 0

    def kill(self) -> None:
        """Kill the process."""

    def terminate(self) -> None:
        """Terminate the process."""


def test_isinstance_rejects_a_double_missing_an_attribute() -> None:
    """Catch a missing member at runtime, which is the presence half of the contract"""
    assert not isinstance(_DoubleMissingStdout(), WorkerProcess)


def test_isinstance_accepts_a_double_with_a_wrong_wait_signature() -> None:
    """Record that the runtime check is blind to signature drift.

    This asserts undesired behaviour on purpose. `@runtime_checkable` verifies
    that a member *exists*, never its type or its arity, so a `wait()` that has
    grown an argument passes. That is the exact shape of the original outage,
    and it is why the child-process type-check cases below are not redundant
    with this one. Deleting them because "isinstance already checks the
    Protocol" would reopen the gap.
    """
    assert isinstance(_DoubleWithWrongWaitSignature(), WorkerProcess), (
        "isinstance no longer accepts a wrong `wait()` signature. If a Python "
        "release closed this gap, that is good news -- but confirm it before "
        "relaxing the static half, and update this test's reasoning."
    )


# --------------------------------------------------------------------------
# Source purity
# --------------------------------------------------------------------------


def test_no_source_module_imports_from_tests() -> None:
    """Keep production free of test-tree imports, which is why the Protocol ships in src/"""
    offenders: list[str] = []
    scanned = 0
    for module in sorted((REPO_ROOT / "src").rglob("*.py")):
        scanned += 1
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported = [node.module]
            if any(name == "tests" or name.startswith("tests.") for name in imported):
                offenders.append(f"{module.relative_to(REPO_ROOT)}:{node.lineno}")

    # Without this the case passes on an empty glob -- a moved source tree would
    # read as "no offenders" rather than as "nothing was checked".
    assert scanned, "No modules were found under src/, so this case checked nothing."
    assert not offenders, (
        f"These modules under src/ import from tests/: {offenders}. The "
        "WorkerProcess Protocol lives in src/ precisely so production never has "
        "to."
    )


# --------------------------------------------------------------------------
# The mypy configuration
# --------------------------------------------------------------------------

PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
NEGATIVE_FIXTURE_DIR = REPO_ROOT / "tests" / "typing_negative"

# Every negative fixture, and the diagnostic each exists to provoke. Pinning the
# SET as well as each member is deliberate: a per-fixture check answers "does
# this one still fail?" and none of them notices a fixture that was added and
# never wired up, or one that was deleted along with its case.
NEGATIVE_FIXTURES: Final = {
    "any_typed_double.py": "explicit-any",
    "double_missing_stdout.py": "assignment",
    "async_mock_double.py": "assert-type",
    "bytearray_payload.py": "arg-type",
}


def _mypy_configuration() -> dict[str, Any]:
    """Read the ``[tool.mypy]`` table out of ``pyproject.toml``.

    Returns:
        The parsed table, including its ``overrides`` array.
    """
    with PYPROJECT_PATH.open("rb") as handle:
        return tomllib.load(handle)["tool"]["mypy"]


def test_mypy_configuration_forbids_inferred_any() -> None:
    """Keep `disallow_any_expr` on the Protocol and doubles modules"""
    guarded = {
        module
        for override in _mypy_configuration().get("overrides", [])
        if override.get("disallow_any_expr")
        for module in override["module"]
    }

    assert guarded == {
        "kindly_web_search_mcp_server.scrape.types",
        "tests.doubles.worker_process",
    }, (
        "The modules carrying `disallow_any_expr` changed. It is the only "
        "setting that catches an `Any` arriving by inference inside the double "
        f"-- `disallow_any_explicit` and `disallow_untyped_defs` do not. Got: {guarded}."
    )


def test_mypy_configuration_excludes_every_negative_fixture() -> None:
    """Keep files that must fail out of the path the job checks"""
    patterns = [re.compile(pattern) for pattern in _mypy_configuration()["exclude"]]
    unexcluded = sorted(
        path.name
        for path in NEGATIVE_FIXTURE_DIR.glob("*.py")
        if path.name != "__init__.py"
        and not any(p.search(path.relative_to(REPO_ROOT).as_posix()) for p in patterns)
    )

    assert not unexcluded, (
        f"{unexcluded} are not excluded from the mypy target. A file that must "
        "fail type-checking cannot sit in the path the job checks, or the job "
        "is red forever."
    )


def test_mypy_configuration_anchors_the_source_path() -> None:
    """Resolve `mypy_path` against this file, not against the caller's directory"""
    mypy_path = _mypy_configuration()["mypy_path"]

    assert mypy_path.startswith("$MYPY_CONFIG_FILE_DIR"), (
        f"mypy_path is {mypy_path!r}. A relative mypy_path resolves against the "
        "mypy process's working directory, not this file's location, so a child "
        "invocation started elsewhere would silently lose it -- and with the "
        "source path lost, WorkerProcess degrades to Any and every conformance "
        "assignment passes vacuously."
    )


def test_mypy_configuration_reports_unused_sections() -> None:
    """Keep the only signal that a per-module key matches nothing"""
    assert _mypy_configuration().get("warn_unused_configs") is True, (
        "warn_unused_configs is off. A mistyped `module` key silently disables "
        "its settings, and a test that merely reads the setting out of this "
        "file still passes."
    )


# --------------------------------------------------------------------------
# The type-check harness
#
# These spawn a child mypy, which is what `subsystem` is defined as covering
# ("needs a real socket or child process"). That marker is this step's explicit
# answer to section 10.4, which pins mypy in the ratchet lockfile only on the
# assumption that this harness stays in the hermetic set. It does not, so mypy
# leaves that extra and that lockfile.
# --------------------------------------------------------------------------

# Diagnostics that mean "mypy could not resolve the code", not "the code is
# wrong". Every case below asserts their absence, because each would otherwise
# let a case pass for the wrong reason: an unresolved import degrades
# `WorkerProcess` to `Any`, under which every conformance assignment succeeds.
IMPORT_FAILURE_CODES: Final = ("import-untyped", "import-not-found")

ERROR_CODE_PATTERN = re.compile(r"\[([a-z][a-z0-9-]+)\]\s*$", re.MULTILINE)


def _child_environment() -> dict[str, str]:
    """Build the environment every child mypy runs under.

    Returns:
        A copy of the current environment, cleaned. ``PYTEST_ADDOPTS`` is
        dropped so an inherited selection cannot change what the child does,
        and ``PYTHONIOENCODING`` is pinned so a Windows runner does not encode
        the child's output with the locale codec while this process decodes it
        with another.
    """
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _run_mypy(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a child mypy against the committed configuration.

    ``cwd`` is pinned to the repository root, belt-and-braces with
    ``$MYPY_CONFIG_FILE_DIR`` in ``mypy_path``: a relative source path would
    otherwise resolve against whatever directory the runner happened to start
    in, and the resulting unresolved import makes every conformance assignment
    pass vacuously.

    Args:
        *args: Paths or flags appended to the invocation. With none, mypy checks
            the ``files`` target declared in ``pyproject.toml``.

    Returns:
        The completed process, with ``stdout`` and ``stderr`` captured as text.

    Raises:
        subprocess.TimeoutExpired: When the child has not exited within 120
            seconds, so a hung child fails one case instead of the whole run.
    """
    return subprocess.run(
        [sys.executable, "-m", "mypy", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env=_child_environment(),
        timeout=120,
        check=False,
    )


def _assert_resolved(result: subprocess.CompletedProcess[str]) -> None:
    """Fail unless mypy actually resolved the code it was pointed at.

    Args:
        result: A completed child mypy run.

    Raises:
        AssertionError: When mypy reported an import diagnostic, which would
            make any other assertion about this run meaningless.
    """
    reported = [code for code in IMPORT_FAILURE_CODES if f"[{code}]" in result.stdout]
    assert not reported, (
        f"mypy reported {reported}, so it could not resolve the code under "
        "check. Under an unresolved import WorkerProcess degrades to Any and "
        f"every conformance assignment passes vacuously.\n{result.stdout}"
    )


@pytest.mark.subsystem
def test_mypy_accepts_the_typed_double_surface() -> None:
    """Type-check the Protocol and its double cleanly, with imports resolved"""
    result = _run_mypy()

    _assert_resolved(result)
    assert result.returncode == 0, (
        f"mypy rejected the committed type-check target.\n{result.stdout}"
    )


@pytest.mark.subsystem
def test_ordinary_target_reports_no_unused_configuration_sections() -> None:
    """Prove every per-module section actually binds to a file mypy processes.

    This is the active half of the mistyped-key guard. Reading
    ``disallow_any_expr`` out of ``pyproject.toml`` proves the setting is
    written down; only this proves it reaches the module. Measured: changing the
    key to ``doubles.worker_process`` leaves mypy at exit 0 with the setting
    silently inert, and the unused-section note is the only signal.
    """
    result = _run_mypy()

    # Without these two, a child that died before checking anything -- mypy
    # absent from the lane, a malformed config, a crash -- writes nothing to
    # stdout and this case passes on an empty string.
    _assert_resolved(result)
    assert result.returncode == 0, f"mypy did not complete.\n{result.stdout}"
    assert "unused section" not in result.stdout, (
        "mypy reported an unused configuration section, so a per-module `module` "
        f"key matches nothing it processed and its settings are inert.\n{result.stdout}"
    )


@pytest.mark.subsystem
def test_unused_configuration_sections_are_reported(tmp_path: Path) -> None:
    """Prove mypy still reports unused sections, which the guard above assumes.

    ``warn_unused_configs`` is documented as requiring ``incremental = false``.
    Measured on mypy 2.3.1 it reports either way, so this project takes the fast
    path and does not disable incremental mode -- which leaves the guard above
    resting on undocumented behaviour. This case is what stops that from
    becoming a silent loss: if a release starts honouring the documentation, the
    note disappears and this goes red, rather than the guard above quietly
    passing forever.

    Args:
        tmp_path: Destination for a throwaway config carrying a key that
            deliberately matches nothing.
    """
    config = tmp_path / "mypy.ini"
    config.write_text(
        "[mypy]\n"
        "python_version = 3.13\n"
        f"mypy_path = {REPO_ROOT / 'src'}\n"
        "warn_unused_configs = True\n"
        "files = tests/doubles\n"
        "\n"
        "[mypy-no.such.module.anywhere]\n"
        "disallow_any_expr = True\n",
        encoding="utf-8",
    )

    result = _run_mypy("--config-file", str(config))

    _assert_resolved(result)
    assert "unused section" in result.stdout, (
        "mypy no longer reports a per-module section that matches nothing, so "
        "test_ordinary_target_reports_no_unused_configuration_sections can no "
        "longer detect a mistyped `module` key. Set `incremental = false` in "
        f"[tool.mypy] and re-measure.\n{result.stdout}"
    )


@pytest.mark.subsystem
@pytest.mark.parametrize(("fixture", "expected_code"), sorted(NEGATIVE_FIXTURES.items()))
def test_mypy_rejects_each_negative_fixture(fixture: str, expected_code: str) -> None:
    """Reject every fixture that exists to fail, by its own discriminating code.

    The code is asserted, never the exit status alone: mypy exits non-zero for a
    syntax error, an unresolved import or a crash just as readily as for the
    diagnostic a fixture exists to provoke.

    Args:
        fixture: File name under ``tests/typing_negative/``.
        expected_code: The diagnostic that fixture must provoke.
    """
    result = _run_mypy(f"tests/typing_negative/{fixture}")

    _assert_resolved(result)
    assert result.returncode != 0, (
        f"mypy accepted {fixture}, which exists to be rejected. A type-check "
        f"job that cannot fail is indistinguishable from no job.\n{result.stdout}"
    )
    assert expected_code in ERROR_CODE_PATTERN.findall(result.stdout), (
        f"{fixture} was rejected, but not for the reason it exists. Expected "
        f"[{expected_code}], got {sorted(set(ERROR_CODE_PATTERN.findall(result.stdout)))}."
        f"\n{result.stdout}"
    )


def test_every_negative_fixture_is_named_by_this_module() -> None:
    """Pin the fixture set, so an unwired fixture cannot sit there proving nothing"""
    on_disk = {
        path.name for path in NEGATIVE_FIXTURE_DIR.glob("*.py") if path.name != "__init__.py"
    }

    assert on_disk == set(NEGATIVE_FIXTURES), (
        "The negative fixtures on disk and the ones this module exercises "
        f"disagree. Unwired: {sorted(on_disk - set(NEGATIVE_FIXTURES))}; "
        f"missing: {sorted(set(NEGATIVE_FIXTURES) - on_disk)}."
    )


@pytest.mark.subsystem
def test_negative_fixtures_are_absent_from_a_whole_tree_run() -> None:
    """Prove `exclude` keeps the fixtures out of a target wider than `files`.

    Deliberately runs over the whole ``tests`` tree rather than the committed
    target. Measured: with ``files`` scoped as it is, deleting ``exclude``
    entirely changes nothing about the ordinary run, so a case pointed there
    would be a duplicate of the one above and could not fail. Over ``tests`` the
    setting is load-bearing -- without it the fixtures are discovered and
    reported, which is what would make a widened type-check target red forever.

    Exit status is deliberately not asserted: the wider tree carries unrelated
    diagnostics in modules this step does not own.
    """
    result = _run_mypy("tests")

    reported = sorted(name for name in NEGATIVE_FIXTURES if name in result.stdout)
    assert not reported, (
        f"{reported} were reported by a whole-tree run, so `exclude` is not "
        "keeping the deliberately-broken fixtures out of a widened target.\n"
        f"{result.stdout}"
    )


@pytest.mark.subsystem
def test_the_any_fixture_is_clean_without_its_inline_directive(tmp_path: Path) -> None:
    """Prove the fixture's own directive is what makes it fail, not something ambient.

    A negative fixture that would fail for some unrelated reason proves nothing
    about the setting it is supposed to demonstrate. Stripping the inline
    ``# mypy:`` line must leave it clean.

    Args:
        tmp_path: Destination for the stripped copy. Safe here specifically
            because this fixture imports nothing from ``src/``, so moving it out
            of the source tree cannot introduce an import diagnostic.
    """
    source = (NEGATIVE_FIXTURE_DIR / "any_typed_double.py").read_text(encoding="utf-8")
    stripped = source.replace("# mypy: disallow-any-explicit\n", "", 1)
    assert stripped != source, "The inline directive this case strips has moved."
    copy = tmp_path / "any_typed_double.py"
    copy.write_text(stripped, encoding="utf-8")

    result = _run_mypy(str(copy))

    _assert_resolved(result)
    assert result.returncode == 0, (
        "The Any-typed fixture still fails with its inline directive removed, "
        "so that directive is not what rejects it and the harness case proves "
        f"nothing about the setting.\n{result.stdout}"
    )
