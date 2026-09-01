"""A double missing ``stdout``, which mypy MUST reject.

This is the original outage, frozen as a fixture. The double in
``tests/test_universal_html_loader.py`` declared ``returncode`` and
``communicate()``; production stopped calling ``communicate()`` and started
reading ``proc.stdout``, and nothing caught the gap until three tests failed at
runtime with ``AttributeError``.

The harness in :mod:`tests.test_worker_process_protocol` asserts that mypy
rejects this file with the ``assignment`` code. That is the acceptance criterion
"removing ``stdout`` from the double fails mypy", proven by a committed file
rather than asserted in prose.

It needs no inline strictness: a missing Protocol member is an error under
mypy's ordinary defaults. It is excluded from the type-check target for the same
reason as its sibling -- a file that must fail cannot sit in the checked path.
"""

from __future__ import annotations

import asyncio

from kindly_web_search_mcp_server.scrape.types import WorkerProcess


class DoubleMissingStdout:
    """A double with every member of the Protocol except ``stdout``.

    Attributes:
        stderr: Standard-error stream.
        pid: The process id reported to callers.
        returncode: Exit status, ``None`` until the process completes.
    """

    def __init__(self) -> None:
        """Build the incomplete double."""
        self.stderr: asyncio.StreamReader | None = None
        self.pid: int = 4242
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Complete the process.

        Returns:
            The exit code.
        """
        return 0

    def kill(self) -> None:
        """Kill the process."""

    def terminate(self) -> None:
        """Terminate the process."""


# The line mypy must reject.
_contract: WorkerProcess = DoubleMissingStdout()
