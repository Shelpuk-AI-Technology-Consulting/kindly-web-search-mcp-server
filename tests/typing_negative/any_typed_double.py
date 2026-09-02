# mypy: disallow-any-explicit
"""A deliberately ``Any``-typed double that mypy MUST reject.

This file exists to prove the type-check job is not vacuous. A job that cannot
fail is indistinguishable from no job, so the harness in
:mod:`tests.test_worker_process_protocol` runs mypy over this file and asserts
both a non-zero exit and the specific diagnostic code.

It is excluded from the ordinary type-check target in ``pyproject.toml`` --
a file that must fail cannot sit in the path the job checks, or the job is red
forever. mypy's ``exclude`` is ignored for a file named explicitly on the command
line, which is how the harness still reaches it.

**The strictness is declared inline, here, rather than as a per-module override.**
Two reasons. The fixture then carries its own reason for failing where a reader
meets it, instead of in a config file three directories away. And ``exclude``
removes a file from discovery but not from configuration, so an override for this
directory sits unused whenever the ordinary target runs -- which
``warn_unused_configs`` correctly reports, and which would drown the signal that
setting exists to give.

``disallow-any-explicit`` is chosen over ``disallow-any-expr`` because it emits
the discriminating code ``explicit-any``. ``disallow-any-expr`` emits ``misc``,
mypy's catch-all bucket shared by dozens of unrelated errors; a harness asserting
``misc`` proves far less than it appears to.
"""

from typing import Any


class AnyTypedDouble:
    """A double that satisfies any Protocol at all, and therefore proves nothing.

    Attributes:
        stdout: Explicitly ``Any``, which is the defect this fixture embodies.
    """

    stdout: Any
