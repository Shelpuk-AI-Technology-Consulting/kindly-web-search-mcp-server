"""Fixture servers spawned or started by the test suite.

A directory named for what it holds, matching ``tests/doubles`` (typed doubles)
and ``tests/child_processes`` (spawnable scripts). Deliberately **not**
``tests/fixtures``: in a pytest suite that word already means
``@pytest.fixture``.

Deliberately outside ``tests/doubles`` even though a fixture server is a kind of
double. ``pyproject.toml`` lists ``tests/doubles`` in the type-check job's
``files``, so a module placed there joins that job's scope silently, by its
location alone. That job is narrow on purpose -- it exists to catch signature
drift on the worker-process Protocol -- and widening it to cover a socket server
is a decision, not a side effect of where a file was put.
"""
