"""Type-only declarations for the worker-process seam.

This module holds the structural contract between ``worker_runner.py`` and the
child process it spawns. It declares no behaviour and imports nothing from this
package, so importing it can never pull in the scraping stack.

It lives under ``src/`` rather than under ``tests/`` for one reason:
``worker_runner._run_worker_command`` annotates the process it spawns with
:class:`WorkerProcess`, and production must never import from the test tree.
Declaring it here also means there is exactly one definition of the shape, which
is the drift this module exists to prevent — a Protocol declared in a test module
would be the second.

The name follows the convention for a type-only module. It shadows the stdlib
``types`` only within this package, and it is unrelated to the type-check CI job
that shares the word. Its one production consumer imports it relatively
(``from .types import WorkerProcess``), matching that module's other
intra-package import; measured, nothing depends on which spelling is used.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkerProcess(Protocol):
    """The surface ``worker_runner._run_worker_command`` consumes from its child.

    Exactly the seven members production reads, and no more. Two independent
    checks hold that now. A literal in :mod:`tests.test_worker_process_protocol`
    fails if this Protocol is padded with a member nothing consumes; and the
    annotation on the spawned process fails if production reads an eighth member
    or loses one of the seven. The second direction was open until the runner
    was annotated, and until then the literal was the only link.

    The consumer is no longer ``fetch_html_via_nodriver``: the runner extraction
    took the spawn, the stream reads and the exit status out of it, and that
    function now touches no process at all.

    ``@runtime_checkable`` is required for the *presence* half of the contract:
    without it ``isinstance`` raises ``TypeError`` rather than answering. It is
    not sufficient on its own — measured, a double whose ``wait()`` takes a
    wrong number of arguments still satisfies ``isinstance``, which is precisely
    the drift that broke the loader tests. The static check is what catches that,
    and both are kept.

    **Every *attribute* member is a read-only property — the four that carry
    state, not the three methods — and that is load-bearing rather than
    stylistic.** Declared as plain attribute annotations, the real
    :class:`asyncio.subprocess.Process` does *not* satisfy this Protocol — mypy
    rejects it with "Protocol member WorkerProcess.returncode expected settable
    variable, got read-only attribute", because ``returncode`` is a read-only
    property on the real type. ``worker_runner._run_worker_command`` annotates
    the process it spawns with this Protocol and is handed a real one, so the
    attribute spelling would have made that annotation impossible. Do not
    "simplify" these back to annotations.

    The cost, recorded so it is not mistaken for an oversight: read-only members
    are covariant, so a double declaring ``returncode: int`` — one that can never
    express "still running" — also satisfies this Protocol. The attribute
    spelling would have rejected that invariantly. Doubles are expected to
    declare ``int | None`` by convention. The double in
    ``tests/doubles/worker_process.py`` is pinned against that narrowing by an
    ``assert_type`` in its conformance block; a double declared outside that
    block is not.
    """

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        """The child's standard-output stream.

        Returns:
            The stream, or ``None`` when the child was spawned without a pipe.
            The type is fixed by the call site: production passes this straight
            into a reader annotated ``asyncio.StreamReader | None``.
        """
        ...

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        """The child's standard-error stream.

        Returns:
            The stream, or ``None`` when the child was spawned without a pipe.
        """
        ...

    @property
    def pid(self) -> int:
        """The child's process id.

        Declared ``int`` rather than ``int | None``, and production was brought
        into line rather than the other way round: ``_terminate_process_tree``
        used to guard ``proc.pid is not None``, which this declaration makes
        dead for every implementer. Measured on the real type as well —
        ``asyncio.subprocess.Process`` assigns ``self.pid`` once in ``__init__``
        from ``transport.get_pid()`` and never clears it.

        Returns:
            The operating-system process id, used to kill the process tree.
        """
        ...

    @property
    def returncode(self) -> int | None:
        """The child's exit status.

        Returns:
            The exit code, or ``None`` while the child is still running. The
            optional half is required, not incidental: the heartbeat loop in
            production is spelled ``while proc.returncode is None``.
        """
        ...

    async def wait(self) -> int:
        """Wait for the child to exit.

        Returns:
            The child's exit code.
        """
        ...

    def kill(self) -> None:
        """Send the child an unconditional kill signal."""
        ...

    def terminate(self) -> None:
        """Ask the child to terminate."""
        ...
