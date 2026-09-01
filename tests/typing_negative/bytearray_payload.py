"""A buffer-typed stream payload, which mypy MUST reject.

This fixture enforces the ``strict_bytes`` consequence that the implementation
plan assigns to this step: mypy 2.x enables ``--strict-bytes`` by default, so a
double handing back ``bytearray`` or ``memoryview`` where ``bytes`` is expected
no longer type-checks.

It exists because the obvious runtime test cannot prove this. Measured:
``asyncio.StreamReader.read()`` returns ``bytes`` whatever you feed it --

.. code-block:: text

   fed bytes      -> read() -> bytes
   fed bytearray  -> read() -> bytes
   fed memoryview -> read() -> bytes

-- so an assertion on the *type read back* can never fail, and the constraint is
enforceable only at the call site, statically. Hence a fixture rather than a
test.

Excluded from the ordinary type-check target, like its siblings.
"""

from __future__ import annotations

from tests.doubles.worker_process import primed_reader

# The line mypy rejects, with code `arg-type`. Under `--no-strict-bytes` -- the
# mypy 1.x default this project deliberately skipped -- it would be accepted.
_reader = primed_reader(bytearray(b"<html>ok</html>"))
