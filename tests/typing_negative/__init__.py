"""Fixtures that must FAIL type-checking.

Every module here is a deliberate defect. They exist so the type-check harness
can prove it is capable of failing: a job that cannot fail is indistinguishable
from no job.

The package is excluded from the ordinary mypy target in ``pyproject.toml`` --
a file that must fail cannot sit in the path the job checks, or the job is red
forever. mypy's ``exclude`` is ignored for a file named explicitly on the command
line, which is how :mod:`tests.test_worker_process_protocol` still reaches them.

Nothing here is imported at run time; pytest collects only ``test_*.py``.
"""
