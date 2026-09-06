"""Helpers for tests that start something real.

A directory named for what it holds, matching ``tests/doubles`` (typed doubles),
``tests/child_processes`` (spawnable scripts) and ``tests/fixture_servers``
(servers the suite starts). Deliberately **not** ``tests/fixtures``: in a pytest
suite that word already means ``@pytest.fixture``.

Deliberately outside ``tests/doubles`` even though a harness is a kind of test
support code. ``pyproject.toml`` lists ``tests/doubles`` in the type-check job's
``files``, so a module placed there joins that job's scope silently, by its
location alone. That job is narrow on purpose, and widening it to cover process
and socket plumbing is a decision rather than a side effect of where a file was
put.

Section 5.4 of ``.system_design/TEST_SUITE.md`` is the specification these
modules implement, and ``tests/test_anti_flake_harness.py`` is their calibration.
"""
