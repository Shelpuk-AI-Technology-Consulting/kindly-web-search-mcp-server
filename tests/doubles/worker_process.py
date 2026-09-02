"""A typed double for the worker-process seam.

:class:`FakeWorkerProcess` implements
:class:`kindly_web_search_mcp_server.scrape.types.WorkerProcess`, importing that
canonical Protocol rather than restating it, so there is one definition of the
shape and not two that can drift.

**Two static mechanisms guard this module, and each catches what the other
cannot.** Both are measured, not assumed:

* ``disallow_any_expr`` catches an ``Any`` arriving *by inference inside this
  module's own body* — ``self.stdout = some_untyped_call()`` is clean without it
  and rejected with it.
* The :func:`typing.assert_type` block at the bottom catches a *vacuous double*.
  This is the mechanism that rejects an ``AsyncMock`` substitution, and it is
  needed because ``disallow_any_expr`` does **not**: ``AsyncMock()`` has type
  ``AsyncMock``, not ``Any``, so an expression of that type is never flagged, and
  after ``_contract: WorkerProcess = AsyncMock()`` the variable is typed
  ``WorkerProcess``. Nothing in the module is ever ``Any``, and the check passes
  while proving nothing. Reading a member *off the concrete double type* is what
  surfaces the ``Any``.

A forced assignment is required at all: declaring a Protocol and a class next to
each other does not make a type checker compare them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, assert_type

from kindly_web_search_mcp_server.scrape.types import WorkerProcess


class FakeWorkerProcess:
    """A concrete, fully annotated stand-in for a spawned worker process.

    Deliberately a real class rather than a mock. A mock's members infer as
    ``Any`` and satisfy every Protocol ever written, which makes the static half
    of the contract vacuous.

    The double models a **lifecycle** rather than a static snapshot:
    ``returncode`` is ``None`` until the process is waited on or killed. A double
    born already exited cannot exercise production's heartbeat loop, which is
    spelled ``while proc.returncode is None``.

    Attributes:
        stdout: Standard-output stream, or ``None`` when no pipe was requested.
        stderr: Standard-error stream, or ``None`` when no pipe was requested.
        pid: The process id reported to callers.
        returncode: Exit status, ``None`` until the process completes.
    """

    def __init__(
        self,
        *,
        stdout: asyncio.StreamReader | None = None,
        stderr: asyncio.StreamReader | None = None,
        pid: int = 4242,
        exit_code: int = 0,
    ) -> None:
        """Build a double, optionally wired to already-primed streams.

        Args:
            stdout: Standard-output stream, or ``None`` for no pipe.
            stderr: Standard-error stream, or ``None`` for no pipe.
            pid: The process id to report.
            exit_code: The status recorded once the process completes.

        Note:
            Streams are passed in rather than built here, and built by
            :func:`primed_reader`. Constructing an
            :class:`asyncio.StreamReader` outside a running event loop raises
            ``DeprecationWarning: There is no current event loop`` on CPython
            3.13 and is slated to become an error, so a double that built its
            own streams could not be constructed from a synchronous test or at
            import time.
        """
        self.stdout: asyncio.StreamReader | None = stdout
        self.stderr: asyncio.StreamReader | None = stderr
        self.pid: int = pid
        self.returncode: int | None = None
        self._exit_code: int = exit_code

    async def wait(self) -> int:
        """Complete the process and report its status.

        Returns:
            The configured exit code.
        """
        self.returncode = self._exit_code
        return self._exit_code

    def kill(self) -> None:
        """Complete the process as if it had been killed."""
        self.returncode = self._exit_code

    def terminate(self) -> None:
        """Complete the process as if it had been asked to terminate."""
        self.returncode = self._exit_code


def primed_reader(payload: bytes) -> asyncio.StreamReader:
    """Build a stream that yields ``payload`` and then reports EOF.

    Call this from inside a running event loop. It is public because the tests
    that repair the loader cases need it to wire a double to real output.

    Args:
        payload: The bytes the stream yields before end-of-file. ``bytes``, not
            a buffer type: mypy 2.x enables ``strict_bytes`` by default, so a
            ``bytearray`` or ``memoryview`` no longer type-checks here.

    Returns:
        A real :class:`asyncio.StreamReader`, so the double exercises the same
        reader production does rather than a hand-rolled stand-in.
    """
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


# --------------------------------------------------------------------------
# The static conformance block.
#
# Guarded by TYPE_CHECKING so it is never evaluated at run time. mypy analyses
# the branch regardless -- it treats the name as True -- and `assert_type` is a
# runtime no-op anyway, so nothing static is lost. Keeping it off the runtime
# path is what matters: these statements read members off the double, so a
# missing member would raise AttributeError while this module is imported and
# abort collection of the entire suite, reporting one defect as "0 tests ran"
# instead of as the single conformance case that owns the claim.
# --------------------------------------------------------------------------

if TYPE_CHECKING:
    # Without an assignment the checker has to verify, the Protocol and the
    # double above are merely two declarations in one file.
    _contract: WorkerProcess = FakeWorkerProcess()

    def _real_process_conforms(process: asyncio.subprocess.Process) -> WorkerProcess:
        """Assert statically that the real process satisfies the Protocol.

        This pins the read-only-property spelling in ``scrape/types.py``:
        declared as plain attribute annotations the Protocol is *not* satisfied
        by the real type, because ``returncode`` is a read-only property on it.
        The later step that annotates production code with this Protocol depends
        on this line holding.

        Args:
            process: A real spawned process.

        Returns:
            The same object, viewed as a :class:`WorkerProcess`.
        """
        return process

    # Member-level assertions, read off the CONCRETE double rather than off the
    # `WorkerProcess`-typed variable above. This is the mechanism that rejects a
    # mock substitution: reading any member off a mock yields `Any`, which
    # `assert_type` refuses. Deleting this block lets a mock pass silently --
    # `disallow_any_expr` does not catch it, as
    # `tests/typing_negative/async_mock_double.py` records.
    _double = FakeWorkerProcess()
    assert_type(_double.stdout, "asyncio.StreamReader | None")
    assert_type(_double.stderr, "asyncio.StreamReader | None")
    assert_type(_double.pid, int)
    assert_type(_double.returncode, "int | None")
    assert_type(_double.kill(), None)
    assert_type(_double.terminate(), None)

    async def _wait_returns_int() -> None:
        """Assert statically that awaiting ``wait()`` yields an ``int``."""
        assert_type(await _double.wait(), int)
