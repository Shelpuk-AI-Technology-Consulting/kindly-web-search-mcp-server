"""Guard the implementation plan's dependency validator.

The validator exists so the plan's prose dependency graph cannot drift from its
per-step declarations. Its first version accepted loose syntax and quietly
misread it -- ``impl E1-1…E1-5`` was understood as two steps rather than five, and
``X-*`` external prerequisites were ignored entirely, which made its "startable
now" count wrong in the direction that looks encouraging.

These tests pin the behaviours that stop it guessing again. They are cheap unit
tests over synthetic plan fragments; the real plan is validated separately by
running the script.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_plan_dag as checker

HEADER = "| ID | Step | Type | Blocked by | Size |\n|---|---|---|---|---|\n"


def plan(*rows: str, externals: str = "") -> str:
    """Build a synthetic plan fragment from step table rows.

    Args:
        rows: Markdown table rows, each declaring one step.
        externals: Optional extra Markdown declaring external prerequisites.

    Returns:
        A document the validator can parse.
    """
    return HEADER + "\n".join(rows) + "\n" + externals


def test_ranges_are_rejected_rather_than_partially_read() -> None:
    """Reject a range instead of silently reading its endpoints

    This is the defect that motivated the rewrite: ``E1-1…E1-5`` names five steps
    and the original parser saw two.
    """
    with pytest.raises(checker.PlanError, match="ranges and wildcards"):
        checker.parse_plan(plan("| **E1-6** | Green | milestone | merge E1-1…E1-5 | S |"))


def test_wildcards_are_rejected() -> None:
    """Reject a wildcard dependency, which the original parser read as none"""
    with pytest.raises(checker.PlanError, match="ranges and wildcards"):
        checker.parse_plan(plan("| **E10-3** | Activate | PR | merge E5-* | S |"))


def test_duplicate_step_ids_are_rejected() -> None:
    """Reject a step id declared twice, which would hide one definition"""
    rows = (
        "| **E0-1** | Deps | PR | — | S |",
        "| **E0-1** | Deps again | PR | — | S |",
    )
    with pytest.raises(checker.PlanError, match="declared more than once"):
        checker.parse_plan(plan(*rows))


def test_unknown_step_type_is_rejected() -> None:
    """Reject a Type outside the recognised set, so non-PR work stays visible"""
    with pytest.raises(checker.PlanError, match="expected one of"):
        checker.parse_plan(plan("| **E0-1** | Deps | task | — | S |"))


def test_unknown_external_prerequisite_is_reported() -> None:
    """Report a dependency on an external prerequisite the plan never declares"""
    steps, externals = checker.parse_plan(
        plan("| **E9-3** | Tests | PR | complete X-9 | M |")
    )
    assert checker.check_references(steps, externals) == [
        "E9-3 depends on undefined X-9"
    ]


def test_external_prerequisites_must_use_complete() -> None:
    """Require `complete` for a non-PR prerequisite

    `impl` and `merge` describe code landing; an admin action or a product
    decision has neither, and the distinction is what stops them being scheduled
    as if they were PRs.
    """
    document = plan(
        "| **E4-2** | Protection | operation | impl X-2 | S |",
        externals="| **X-2** | Admin authority | E4-2 | admin |\n",
    )
    steps, externals = checker.parse_plan(document)

    problems = checker.check_references(steps, externals)

    assert problems == ["E4-2 declares 'impl X-2', but X-2 is an external "
                        "prerequisite and must use 'complete'"]


def test_declared_external_prerequisite_resolves() -> None:
    """Accept a well-formed `complete` dependency on a declared prerequisite"""
    document = plan(
        "| **E4-2** | Protection | operation | complete X-2 | S |",
        externals="| **X-2** | Admin authority | E4-2 | admin |\n",
    )
    steps, externals = checker.parse_plan(document)

    assert checker.check_references(steps, externals) == []


def test_cycle_is_detected() -> None:
    """Detect a dependency cycle rather than reporting a short ordering"""
    rows = (
        "| **E2-1** | A | PR | impl E2-2 | S |",
        "| **E2-2** | B | PR | impl E2-1 | S |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    ordered, stuck = checker.topological_order(steps)

    assert ordered == []
    assert stuck == ["E2-1", "E2-2"]


def test_steps_downstream_of_a_cycle_are_reported_too() -> None:
    """Report a step blocked by a cycle, which can never become ready"""
    rows = (
        "| **E2-1** | A | PR | impl E2-2 | S |",
        "| **E2-2** | B | PR | impl E2-1 | S |",
        "| **E7-2** | C | PR | impl E2-2 | M |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    _, stuck = checker.topological_order(steps)

    assert stuck == ["E2-1", "E2-2", "E7-2"]


def test_backward_numbered_dependencies_are_legal() -> None:
    """Allow a lower-numbered step to depend on a higher-numbered one

    E1-4 depends on E2-1 in the real plan; numbering is not ordering.
    """
    rows = (
        "| **E1-4** | Repair | PR | impl E2-1 | M |",
        "| **E2-1** | Protocol | PR | — | M |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    ordered, stuck = checker.topological_order(steps)

    assert stuck == []
    assert ordered.index("E2-1") < ordered.index("E1-4")


def test_longest_chain_counts_the_full_path() -> None:
    """Measure depth from a topological ordering, not from blocker counts

    The original implementation sorted by number of blockers and under-reported a
    nine-step chain as seven.
    """
    rows = (
        "| **E0-1** | A | PR | — | S |",
        "| **E0-2** | B | PR | impl E0-1 | S |",
        "| **E1-2** | C | PR | impl E0-2 | M |",
        "| **E1-3** | D | PR | impl E1-2 | M |",
        "| **E1-6** | E | milestone | merge E1-3 | S |",
    )
    steps, _ = checker.parse_plan(plan(*rows))
    ordered, _ = checker.topological_order(steps)

    depth, chain = checker.longest_chain(steps, ordered)

    assert depth == 5
    assert chain == ["E0-1", "E0-2", "E1-2", "E1-3", "E1-6"]


def test_file_claimed_by_two_steps_is_reported() -> None:
    """Reject the same test file being owned by two migration batches"""
    document = (
        "Files: `tests/test_serper_unit.py`, `tests/test_tavily_unit.py`\n"
        "Files: `tests/test_serper_unit.py`\n"
    )

    problems = checker.check_file_ownership(document)

    assert problems == ["file tests/test_serper_unit.py is claimed by 2 steps"]


def test_distinct_files_across_batches_are_accepted() -> None:
    """Accept batches whose file lists do not overlap"""
    document = (
        "Files: `tests/test_serper_unit.py`\n"
        "Files: `tests/test_tavily_unit.py`\n"
    )

    assert checker.check_file_ownership(document) == []


def test_real_plan_is_valid() -> None:
    """Keep the committed plan passing its own validator"""
    assert checker.main([str(checker.DEFAULT_PLAN)]) == 0
