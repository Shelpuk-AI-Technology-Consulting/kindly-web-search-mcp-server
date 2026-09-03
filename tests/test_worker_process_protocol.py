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
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kindly_web_search_mcp_server.scrape.types import WorkerProcess
from tests.doubles.worker_process import FakeWorkerProcess, primed_reader

# The exact surface `worker_runner._run_worker_command` reads off the child
# process. Kept as a literal so padding the Protocol with a member production
# never consumes fails here rather than passing unnoticed.
#
# The consumer used to be `fetch_html_via_nodriver`, and the rename is not
# cosmetic. That function no longer touches a process at all -- the runner
# extraction took the spawn, the stream reads and the exit status with it -- so
# the four loader tests that used to hand `FakeWorkerProcess` to production now
# double the runner instead.
#
# This literal is no longer the ONLY thing tying the double's shape to what
# production reads. It was, between the extraction and the annotation, and
# nothing failed if the two diverged. `_run_worker_command` now annotates the
# process it spawns with the Protocol, so the seam cases at the bottom of this
# module catch a divergence that this literal cannot see: production growing an
# eighth read. The two check opposite directions and neither is redundant --
# this one fails when the PROTOCOL gains a member nothing consumes, which no
# amount of type-checking production would notice.
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


#: Modules permitted to carry a class mentioning the whole worker-process
#: surface. Everything else must import `FakeWorkerProcess`.
#:
#: Exactly two, and both are real candidates -- verified by running the detector
#: with an empty allow-list, which flags these and nothing else. The
#: `tests/typing_negative/` fixtures are *not* listed: one omits `stdout` and the
#: other declares no class, so neither is ever a candidate, and listing them
#: would be a silently inert entry that a rename could never invalidate.
CANONICAL_DOUBLE_MODULES: Final = (
    # The canonical double.
    "tests/doubles/worker_process.py",
    # This module's own deliberately-wrong doubles, which exist to prove the
    # runtime checks can fail.
    "tests/test_worker_process_protocol.py",
)


def _classes_mentioning_the_whole_surface(source: str, filename: str) -> list[str]:
    """Find classes that name every member of the worker-process surface.

    Collects, per class, the names bound as class attributes or methods plus
    every ``self.X`` appearing anywhere in a method body. That last part
    harvests attribute **reads** as well as assignments, so this is deliberately
    over-inclusive: a class that merely reads all four attributes and defines
    the three methods is reported. Over-inclusive is the safe direction for a
    guard whose miss is a silent second definition.

    Args:
        source: Python source text to scan.
        filename: Name used in the parse error, for a legible failure.

    Returns:
        The names of matching classes, in source order.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.ClassDef):
            continue
        functions = [
            body
            for body in node.body
            if isinstance(body, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        mentioned = {function.name for function in functions}
        mentioned |= {
            target.attr
            for function in functions
            for target in ast.walk(function)
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }
        mentioned |= {
            body.target.id
            for body in node.body
            if isinstance(body, ast.AnnAssign) and isinstance(body.target, ast.Name)
        }
        mentioned |= {
            target.id
            for body in node.body
            if isinstance(body, ast.Assign)
            for target in body.targets
            if isinstance(target, ast.Name)
        }
        if CONSUMED_SURFACE <= mentioned:
            found.append(node.name)
    return found


#: Synthetic sources pinning what the detector does and does not reach.
#:
#: The second row is the point of the pair, and it is uncomfortable: the double
#: that actually broke the loader tests declared `returncode` and
#: `communicate()` only, so **this guard would not have caught the outage it was
#: written after.** It catches a *whole-surface* redeclaration, which is the
#: shape a well-meaning author writes when they reimplement the double rather
#: than import it. A partial or split stand-in is out of reach, and no wording
#: in this module should imply otherwise.
DETECTOR_CASES: Final = (
    (
        "whole-surface copy",
        """
class _Copy:
    def __init__(self) -> None:
        self.stdout = None
        self.stderr = None
        self.pid = 1
        self.returncode = None

    async def wait(self) -> int: return 0
    def kill(self) -> None: ...
    def terminate(self) -> None: ...
""",
        True,
    ),
    (
        "the original _FakeProc, which this guard does NOT reach",
        """
class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"<html>ok</html>", b""
""",
        False,
    ),
)


@pytest.mark.parametrize(("label", "source", "expected"), DETECTOR_CASES)
def test_second_double_detector_reports_only_a_whole_surface_copy(
    label: str, source: str, expected: bool
) -> None:
    """Pin what the second-double detector reaches, in both directions.

    Without this the guard below has no way to fail: its only other
    anti-vacuity assertion counts modules scanned, which stays true however
    broken the detector is.

    Args:
        label: Human-readable name for the planted shape.
        source: The synthetic module source.
        expected: Whether the detector should report a class.
    """
    reported = bool(_classes_mentioning_the_whole_surface(source, f"<{label}>"))

    assert reported is expected, (
        f"The second-double detector {'missed' if expected else 'now flags'} "
        f"{label!r}. If this is a deliberate widening, update DETECTOR_CASES and "
        "the wording that describes the guard's reach in this module, "
        ".github/review/rules/python-tests.md and TEST_SUITE.md section 8."
    )


def test_no_test_module_declares_a_second_worker_double() -> None:
    """Keep exactly one whole-surface definition of the worker-process shape.

    The outage this Protocol exists to prevent was a hand-rolled double drifting
    from production. Repairing the three loader tests removed the last three
    copies; nothing but this stops a fourth appearing, because a test module is
    not inside the mypy target and a convention is not a check.

    **Its reach is a whole-surface redeclaration, and no more.** The double that
    caused the outage named only `returncode` and `communicate()` and would not
    be reported here -- see `DETECTOR_CASES`. What this catches is the shape a
    later author writes when they reimplement the canonical double instead of
    importing it.
    """
    offenders: list[str] = []
    scanned = 0
    for module in sorted((REPO_ROOT / "tests").rglob("*.py")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        if relative in CANONICAL_DOUBLE_MODULES:
            continue
        scanned += 1
        offenders.extend(
            f"{relative}::{name}"
            for name in _classes_mentioning_the_whole_surface(
                module.read_text(encoding="utf-8"), str(module)
            )
        )

    assert scanned, "No test modules were scanned, so this case checked nothing."
    assert not offenders, (
        f"These classes redeclare the whole worker-process surface: {offenders}. "
        "Import FakeWorkerProcess from tests/doubles/worker_process.py instead -- "
        "a second definition of this shape is the drift the Protocol exists to "
        "prevent."
    )


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


RUNNER_PATH = (
    REPO_ROOT / "src" / "kindly_web_search_mcp_server" / "scrape" / "worker_runner.py"
)


def test_production_annotates_the_spawned_process_with_the_protocol() -> None:
    """Prove production names the Protocol on the value it spawns.

    The static cases below spawn a child mypy and are ``subsystem``-marked, so a
    lane running only the fast set would notice nothing if the annotation were
    deleted. This is the hermetic half, and it costs one AST parse.

    It asserts the *annotation*, not the import. An ``if TYPE_CHECKING:`` import
    with no use satisfies "the module imports WorkerProcess", and so does an
    import beside a signature somebody forgot to annotate; neither makes mypy
    compare the Protocol with what production reads.
    """
    source = RUNNER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RUNNER_PATH))

    runner = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_worker_command"
        ),
        None,
    )
    assert runner is not None, "_run_worker_command has moved or been renamed."

    annotated = [
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "proc"
        and isinstance(node.annotation, ast.Name)
        and node.annotation.id == "WorkerProcess"
    ]
    assert annotated, (
        "The process _run_worker_command spawns is not annotated "
        "`WorkerProcess`. Without that annotation mypy checks production's "
        "reads against the concrete asyncio type, the Protocol constrains "
        "nothing, and the only thing tying the double's shape to what "
        "production consumes is the CONSUMED_SURFACE literal above -- which is "
        "a literal, not a check."
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


def test_mypy_target_includes_the_worker_runner() -> None:
    """Keep the annotated seam inside the checked target.

    The annotation is only worth its diff while mypy reads it. Dropping the
    module from ``files`` would leave every static case below checking a file
    that no longer contains the seam, and each of them would go green.
    """
    files = _mypy_configuration()["files"]
    expected = "src/kindly_web_search_mcp_server/scrape/worker_runner.py"

    assert expected in files, (
        f"{expected!r} is not in [tool.mypy] files, which is now {files}. "
        "Outside the target, production can read a member the Protocol never "
        "declares and nothing reports it."
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
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        env=_child_environment(),
        timeout=120,
        check=False,
    )

    # mypy always writes to stdout when it runs at all, even to say "Success".
    # Silence plus a non-zero exit means it never started -- overwhelmingly
    # because the lane installed something narrower than `.[dev]`, which is where
    # mypy now lives. Caught here rather than per case: the cases that assert an
    # *absence* in stdout are satisfied by an empty string, so this failure would
    # otherwise show up as a handful of blank assertion messages.
    if result.returncode != 0 and not result.stdout.strip():
        raise AssertionError(
            "mypy produced no output and exited "
            f"{result.returncode}, so it never ran. mypy is in the `dev` extra "
            "only -- it left `ratchet` when this harness was marked `subsystem` "
            f"-- so this lane must install `.[dev]` or wider.\n{result.stderr}"
        )
    return result


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
        "every conformance assignment passes vacuously. Note this reports an "
        "unresolvable or unannotated import, not a missing mypy -- _run_mypy "
        f"catches that.\n{result.stdout}\n{result.stderr}"
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
def test_mypy_accepts_the_committed_target_for_windows(tmp_path: Path) -> None:
    """Type-check the Windows-only branches a native run declares unreachable.

    This is the cost of narrowing ``_subprocess_launch_options`` on
    ``sys.platform`` rather than silencing the ``STARTUPINFO`` lookup, and it is
    paid here rather than left for the type-check job to discover. Measured:
    mypy does not check code it considers unreachable, so on a Linux runner a
    deliberate ``str``-into-``int`` error inside that function's Windows branch
    is not reported, and under ``--platform win32`` it is. Without this case
    that branch is unchecked on every runner this project currently has.

    Note what it does *not* cover: ``_terminate_process_tree``'s Windows branch
    guards on ``os.name``, which mypy cannot narrow, so the native run reads it
    and this run adds nothing there. That asymmetry is deliberate and is pinned
    by ``test_each_platform_guard_uses_the_spelling_its_checking_needs``.

    Args:
        tmp_path: Home for this run's cache. ``--platform`` is cache-affecting,
            so sharing ``.mypy_cache`` with the native runs makes every run cold
            in both directions -- permanently, including a developer's plain
            ``mypy``. Measured: 0.16s / 1.89s / 2.12s / 1.90s alternating on a
            shared cache, against 0.17s / 0.17s on separate ones. That would
            silently reverse the reasoning recorded against ``incremental`` in
            ``pyproject.toml``. A ``tmp_path`` leaves nothing untracked behind.
    """
    result = _run_mypy("--platform", "win32", "--cache-dir", str(tmp_path / "cache"))

    _assert_resolved(result)
    assert result.returncode == 0, (
        "mypy rejected the committed target under --platform win32. The Windows "
        "branch of _subprocess_launch_options is unreachable on a native run, "
        f"so this is the only case that reads it.\n{result.stdout}"
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
def test_the_bytearray_fixture_is_clean_without_strict_bytes() -> None:
    """Prove `strict_bytes` is what rejects the buffer payload, not something else.

    The sibling vacuity check strips a fixture's own inline directive. This one
    cannot: the ``bytearray`` fixture rests on a mypy *default* rather than on a
    directive it carries, so the equivalent proof is to invert the default and
    watch the diagnostic disappear. Without this, the case asserting ``arg-type``
    could be passing for any number of unrelated reasons.

    ``--no-strict-bytes`` is the mypy 1.x behaviour this project deliberately
    skipped, so this also records what the version bound bought.
    """
    result = _run_mypy("--no-strict-bytes", "tests/typing_negative/bytearray_payload.py")

    _assert_resolved(result)
    assert result.returncode == 0, (
        "The bytearray fixture still fails with --no-strict-bytes, so "
        "`strict_bytes` is not what rejects it and the arg-type case proves "
        f"nothing about mypy 2.x's default.\n{result.stdout}"
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


# --------------------------------------------------------------------------
# The seam itself: production's consumption checked against the Protocol
#
# This is the direction the Protocol harness above cannot reach. An
# introspection test sees only the Protocol, so padding it with a member nothing
# reads fails -- but production growing an eighth member, or dropping one of the
# seven, does not. The annotation on the spawned process is what closes that,
# and the mutations below are what prove it closed.
# --------------------------------------------------------------------------


def _read_a_member_the_protocol_never_declares(source: str) -> str:
    """Make the seam read ``proc.stdin``, which the Protocol does not declare.

    Args:
        source: ``worker_runner.py``'s text.

    Returns:
        The mutated text.
    """
    anchor = "    stdout_state: _StdoutAccumulator | None = None"
    return source.replace(anchor, f"    _undeclared = proc.stdin\n{anchor}", 1)


def _drop_a_member_the_seam_reads(source: str) -> str:
    """Delete ``stdout`` from the Protocol outright.

    A deletion, not a rename. Renaming does two things at once -- the Protocol
    loses a member the seam reads *and* gains one no real process has -- which is
    the next mutation's drift, with its diagnostics. Measured: the rename
    reports four errors and is indistinguishable from that case; the deletion
    reports one.

    Sliced between two signature anchors rather than swapped as one literal, so
    the case is not coupled to the member's docstring prose.

    Args:
        source: ``types.py``'s text.

    Returns:
        The mutated text.
    """
    start = source.index("    @property\n    def stdout(self)")
    end = source.index("    @property\n    def stderr(self)")
    return source[:start] + source[end:]


def _add_a_member_no_real_process_has(source: str) -> str:
    """Grow the Protocol by a member ``asyncio.subprocess.Process`` cannot supply.

    Args:
        source: ``types.py``'s text.

    Returns:
        The mutated text.
    """
    anchor = "    @property\n    def stdout(self)"
    addition = (
        "    @property\n"
        "    def absent_from_a_real_process(self) -> int:\n"
        '        """Declared by nobody, so a real process cannot satisfy it."""\n'
        "        ...\n"
        "\n"
    )
    return source.replace(anchor, addition + anchor, 1)


# Each entry: the file it edits, how it edits it, and the EXACT set of error
# codes it must provoke. Exact rather than membership, because the sets are what
# prove the three mutations do different work: the first two report one code
# from one site, the third also reports `arg-type` where `_run_pipe_probe` hands
# its concrete process to `_terminate_process_tree`. A mutation quietly
# collapsing onto another's diagnostics is the failure this pins.
SEAM_MUTATIONS: Final = {
    "production_reads_a_member_the_protocol_never_declares": (
        "worker_runner.py",
        _read_a_member_the_protocol_never_declares,
        frozenset({"attr-defined"}),
    ),
    "the_protocol_drops_a_member_production_reads": (
        "types.py",
        _drop_a_member_the_seam_reads,
        frozenset({"attr-defined"}),
    ),
    "the_protocol_grows_a_member_a_real_process_lacks": (
        "types.py",
        _add_a_member_no_real_process_has,
        frozenset({"arg-type", "assignment"}),
    ),
}


def _as_ini_value(key: str, value: Any) -> str:
    """Render a value read from ``pyproject.toml`` as mypy expects it in an INI.

    Args:
        key: The option's name, which decides how a list is joined.
        value: A scalar or list taken from the committed ``[tool.mypy]`` table.

    Returns:
        The INI spelling.
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, list):
        # `exclude` is a single regular expression in INI form -- the array is a
        # TOML affordance. Comma-joining two entries would produce a valid
        # regex meaning something else, accepted in silence. Every other
        # list-valued mypy option really is comma-separated.
        separator = "|" if key == "exclude" else ", "
        return separator.join(str(item) for item in value)
    return str(value)


def _copy_configuration(destination: Path) -> Path:
    """Derive the copy's mypy configuration from the committed one.

    Written rather than hand-authored on purpose. A hand-authored config is a
    second, unchecked declaration of the type-check settings: the committed
    table could later gain a setting that changes what the seam check means --
    ``strict``, or extending ``disallow_any_expr`` to this module -- and these
    cases would go on exercising a configuration nobody ships, staying green.
    That is the failure this module opens by naming: a literal is not a check.

    Everything is carried over except ``files`` and ``mypy_path``, which must
    point at the copy, and the per-module overrides for modules outside the
    copy's build. ``tests/doubles`` is deliberately not in that build: with the
    double present the third mutation also breaks ``FakeWorkerProcess``'s own
    ``_contract`` assignment -- a check the Protocol harness already ships --
    and the case could no longer tell its own claim from that one. Measured: 5
    errors across 2 files with it, 3 in 1 file without.

    Args:
        destination: Directory holding the copied tree.

    Returns:
        The written config file's path.
    """
    committed = _mypy_configuration()
    target = (
        destination
        / "src"
        / "kindly_web_search_mcp_server"
        / "scrape"
        / "worker_runner.py"
    )

    lines = ["[mypy]"]
    for key, value in sorted(committed.items()):
        if key in {"files", "mypy_path", "overrides"}:
            continue
        lines.append(f"{key} = {_as_ini_value(key, value)}")
    lines.append(f"mypy_path = {destination / 'src'}")
    lines.append(f"files = {target}")

    for override in committed.get("overrides", []):
        settings = {k: v for k, v in override.items() if k != "module"}
        for module in override["module"]:
            # An override for a module outside this build would match nothing,
            # and `warn_unused_configs` -- carried over above -- would report it.
            if not module.startswith("kindly_web_search_mcp_server."):
                continue
            lines.append("")
            lines.append(f"[mypy-{module}]")
            lines.extend(
                f"{k} = {_as_ini_value(k, v)}" for k, v in sorted(settings.items())
            )

    config = destination / "mypy.ini"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _copy_source_tree(destination: Path) -> Path:
    """Copy ``src/`` so a mutation never touches the repository.

    Args:
        destination: An empty directory, normally pytest's ``tmp_path``.

    Returns:
        The copied ``src`` root.
    """
    copied = destination / "src"
    shutil.copytree(REPO_ROOT / "src", copied)
    return copied


def _run_mypy_on_copy(destination: Path) -> subprocess.CompletedProcess[str]:
    """Type-check the copied runner against the copy's own Protocol.

    The committed configuration cannot simply be reused, and that is measured
    rather than assumed. ``_run_mypy`` pins the child's ``cwd`` to the
    repository root and the committed ``mypy_path`` is
    ``$MYPY_CONFIG_FILE_DIR/src`` -- the *repository's*. Under it, only the file
    named on the command line comes from the copy: with the copy's ``types.py``
    mutated, mypy reported ``Success`` and ``-v`` showed it parsing the
    repository's ``types.py``. Anchoring ``mypy_path`` at the copy is what fixes
    that, and it is sufficient on its own -- measured, an absolute import
    resolves to the copy too, so production's relative import of the Protocol is
    a second line of defence and not the mechanism.

    Args:
        destination: Directory holding the copied tree.

    Returns:
        The completed child mypy run.
    """
    return _run_mypy(
        "--config-file",
        str(_copy_configuration(destination)),
        # A cache shared with the repository's own runs would answer from the
        # unmutated tree.
        "--cache-dir",
        str(destination / "cache"),
    )


@pytest.mark.subsystem
def test_the_copied_tree_type_checks_cleanly(tmp_path: Path) -> None:
    """Control for the mutation cases: prove an unmutated copy is clean.

    Its job is narrower than it looks. It is not what proves the copy is on
    mypy's path -- the two ``types.py`` mutations are, since they report nothing
    at all if mypy reads the repository's Protocol instead, and the remaining
    mutation asserts the reported path itself. What this excludes is the other
    direction: a copy broken for some unrelated reason, under which every
    mutation case would "detect" an error that was there all along.

    Each case builds its own copy, so this control speaks for the machinery, not
    for one shared tree that later cases inherit.

    The unused-section assertion is what proves the *derived* configuration
    actually binds: the copied override is keyed on a module that must be in the
    build, and mypy reports the section as unused if it is not.

    Args:
        tmp_path: Destination for the copied tree.
    """
    _copy_source_tree(tmp_path)

    result = _run_mypy_on_copy(tmp_path)

    _assert_resolved(result)
    assert result.returncode == 0, (
        "An unmutated copy of src/ does not type-check, so the mutation cases "
        "below cannot attribute their errors to the mutation rather than to the "
        f"copy.\n{result.stdout}"
    )
    assert "unused section" not in result.stdout, (
        "The configuration derived from the committed [tool.mypy] carries a "
        "per-module section that binds to nothing in the copy's build, so its "
        "settings are inert here and this run is not the committed one.\n"
        f"{result.stdout}"
    )
    # Measured: mypy reports an unrecognised option name, or a value it cannot
    # parse, as `<config>: [mypy]: ...` and still EXITS 0. So neither the exit
    # status above nor the unused-section guard -- which itself depends on
    # `warn_unused_configs` having been carried across correctly -- can see a
    # setting this derivation renders wrongly.
    #
    # Measured a second time, because the obvious spelling of this assertion is
    # itself a guard that can never fire: those diagnostics go to STDERR, while
    # every other assertion in this module reads stdout.
    assert ": [mypy]" not in result.stderr, (
        "The derived configuration produced a config diagnostic, so a key from "
        "the committed [tool.mypy] did not survive `_as_ini_value` and the copy "
        f"is not running the committed settings.\n{result.stderr}"
    )


@pytest.mark.subsystem
@pytest.mark.parametrize(("label", "mutation"), sorted(SEAM_MUTATIONS.items()))
def test_mypy_rejects_each_seam_mutation(
    label: str,
    mutation: tuple[str, Callable[[str], str], frozenset[str]],
    tmp_path: Path,
) -> None:
    """Prove the annotation makes production's consumption a checked claim.

    Three drifts, one per direction the seam can fail in: production reading a
    member the Protocol never declared, the Protocol losing a member production
    reads, and the Protocol growing one a real ``asyncio.subprocess.Process``
    cannot supply.

    Error *codes* are asserted, never the exit status: mypy exits non-zero for a
    syntax error, an unresolved import or a crash just as readily. And the
    reported path must fall inside the copy -- a diagnostic against the
    repository would mean the mutation was never read.

    Args:
        label: The drift being simulated.
        mutation: File to edit, the edit, and the exact codes it must provoke.
        tmp_path: Destination for the copied tree.
    """
    filename, mutate, expected_codes = mutation
    copied = _copy_source_tree(tmp_path)
    target = copied / "kindly_web_search_mcp_server" / "scrape" / filename
    source = target.read_text(encoding="utf-8")
    mutated = mutate(source)

    # A mutation that matched nothing leaves a clean tree, and a case asserting
    # an error would then fail with a message blaming the seam for a stale
    # anchor.
    assert mutated != source, (
        f"Mutation {label!r} changed nothing in {filename}, so its anchor has "
        "moved. The case cannot simulate the drift it names until that is fixed."
    )
    target.write_text(mutated, encoding="utf-8")

    result = _run_mypy_on_copy(tmp_path)

    _assert_resolved(result)
    reported = set(ERROR_CODE_PATTERN.findall(result.stdout))
    assert reported == set(expected_codes), (
        f"Mutation {label!r} was expected to report exactly "
        f"{sorted(expected_codes)}; mypy reported {sorted(reported) or 'nothing'}. "
        "With the seam annotated this drift must fail the type check -- if it "
        "reports nothing, production and the Protocol can diverge with the "
        "suite green; if it reports a different set, this mutation has "
        f"collapsed onto another's drift.\n{result.stdout}"
    )
    assert str(tmp_path) in result.stdout, (
        "mypy reported no diagnostic inside the copied tree, so it checked the "
        f"repository's own sources and the mutation was never read.\n{result.stdout}"
    )
