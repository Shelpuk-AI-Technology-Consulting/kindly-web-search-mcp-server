"""A mock substituted for the typed double, which mypy MUST reject.

This fixture exists because the obvious mechanism does not work, and the
measurement is worth freezing where someone will find it.

``AsyncMock`` satisfies every Protocol ever written: its members infer as
``Any`` through typeshed's ``__getattr__``. The natural defence is
``disallow_any_expr`` -- and it does **not** fire here. ``AsyncMock()`` has type
``AsyncMock``, not ``Any``, so the expression is never flagged, and after the
conformance assignment the variable is typed ``WorkerProcess``. Measured:

.. code-block:: text

   _contract: WorkerProcess = AsyncMock()
   mypy --disallow-any-expr  ->  Success: no issues found in 1 source file

What does work is reading a member off the **concrete** double type, which is
what :func:`typing.assert_type` does below. That is why
``tests/doubles/worker_process.py`` carries an ``assert_type`` block, and why
deleting it would let a mock substitution pass silently.

Excluded from the ordinary type-check target, like its siblings.
"""

from __future__ import annotations

import asyncio  # noqa: F401  (referenced by the assert_type target below)
from typing import assert_type
from unittest.mock import AsyncMock

from kindly_web_search_mcp_server.scrape.types import WorkerProcess

# Deliberately NOT an error. Recorded so the next reader does not conclude the
# conformance assignment alone is doing the work.
_contract: WorkerProcess = AsyncMock()

# This is the line mypy rejects, with code `assert-type`.
_double = AsyncMock()
assert_type(_double.stdout, "asyncio.StreamReader | None")
