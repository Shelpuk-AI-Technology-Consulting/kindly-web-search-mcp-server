"""Type-only declarations for the worker-process seam.

This module holds the structural contract between ``universal_html.py`` and the
child process it spawns. It declares no behaviour and imports nothing from this
package, so importing it can never pull in the scraping stack.

It lives under ``src/`` rather than under ``tests/`` for one reason: a later step
annotates production code with :class:`WorkerProcess`, and production must never
import from the test tree. Declaring it here also means there is exactly one
definition of the shape, which is the drift this module exists to prevent — a
Protocol declared in a test module would be the second.

The name follows the convention for a type-only module. It shadows the stdlib
``types`` only for absolute-import resolution inside this package, which this
project uses throughout, and it is unrelated to the type-check CI job that
shares the word.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkerProcess(Protocol):
    """The surface ``fetch_html_via_nodriver`` consumes from its child process.

    Exactly the seven members production reads, and no more. The set is asserted
    against a literal in :mod:`tests.test_worker_process_protocol`, so padding it
    with a member nothing consumes fails the suite.

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
    property on the real type. A later step annotates production code with this
    Protocol and passes it a real process, so the attribute spelling would have
    made that step impossible. Do not "simplify" these back to annotations.

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
