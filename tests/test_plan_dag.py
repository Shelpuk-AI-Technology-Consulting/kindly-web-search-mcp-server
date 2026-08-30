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


def test_merge_against_a_non_pr_target_is_rejected() -> None:
    """Reject `merge` on a milestone, which has no PR to land

    The kinds carry scheduling meaning, so a mislabelled one silently changes
    what the plan claims can be parallelised.
    """
    rows = (
        "| **E1-6** | Green | milestone | — | S |",
        "| **E4-2** | Split jobs | PR | merge E1-6 | M |",
    )
    steps, externals = checker.parse_plan(plan(*rows))

    assert checker.check_references(steps, externals) == [
        "E4-2 declares 'merge E1-6', but E1-6 is a milestone and has no PR to "
        "land; use 'complete'"
    ]


def test_complete_against_a_pr_target_is_rejected() -> None:
    """Reject `complete` on a PR, which does have a merge to wait on"""
    rows = (
        "| **E2-1** | Protocol | PR | — | M |",
        "| **E1-4** | Repair | PR | complete E2-1 | M |",
    )
    steps, externals = checker.parse_plan(plan(*rows))

    assert checker.check_references(steps, externals) == [
        "E1-4 declares 'complete E2-1', but E2-1 is a PR; use 'merge'"
    ]


def test_impl_against_an_external_prerequisite_is_allowed() -> None:
    """Accept `impl` on a decision that must be made before authoring

    X-1 is the outbound-policy decision: the security tests cannot be written
    until it is made, which is exactly what `impl` means.
    """
    document = plan(
        "| **E9-2** | Outbound tests | PR | impl X-1 | M |",
        externals="| **X-1** | Outbound policy | E9-2 | maintainer |\n",
    )
    steps, externals = checker.parse_plan(document)

    assert checker.check_references(steps, externals) == []


def test_merge_against_an_external_prerequisite_is_rejected() -> None:
    """Reject `merge` on an external prerequisite, which never lands as a PR"""
    document = plan(
        "| **E4-5** | Protection | operation | merge X-2 | S |",
        externals="| **X-2** | Admin authority | E4-5 | admin |\n",
    )
    steps, externals = checker.parse_plan(document)

    assert checker.check_references(steps, externals) == [
        "E4-5 declares 'merge X-2', but X-2 is a external and has no PR to land; "
        "use 'complete'"
    ]


def test_malformed_step_row_is_reported_not_skipped() -> None:
    """Report a row that looks like a step but is missing a column

    Such a row silently fails the table regex and disappears from the graph --
    the same class of defect as a mis-parsed dependency, where the validator
    passes while the plan is wrong.
    """
    document = HEADER + "| **E0-1** | Deps | PR |\n"
    steps, _ = checker.parse_plan(document)

    problems = checker.check_row_syntax(document, set(steps))

    assert problems == [
        "row for E0-1 looks like a step but did not parse — check its columns"
    ]


def test_empty_files_declaration_is_rejected() -> None:
    """Reject a Files: line that claims nothing, leaving a batch unowned"""
    assert checker.check_file_ownership("Files:  \n") == [
        "a Files: line declares no files"
    ]


def test_claimed_file_that_does_not_exist_is_reported(tmp_path) -> None:
    """Reject a claim on a path that is not in the tree"""
    (tmp_path / "tests").mkdir()

    problems = checker.check_file_ownership("Files: `tests/test_gone.py`\n", tmp_path)

    assert "claimed file tests/test_gone.py does not exist" in problems


def test_unclaimed_unittest_file_is_reported(tmp_path) -> None:
    """Reject leaving a unittest-style file out of every migration batch"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_legacy.py").write_text(
        "import unittest\n", encoding="utf-8"
    )

    assert checker.check_file_ownership("Files: `tests/test_legacy.py`\n", tmp_path) == []

    problems = checker.check_file_ownership("Files: `tests/test_other.py`\n", tmp_path)
    assert (
        "unittest-style tests/test_legacy.py is claimed by no migration batch"
        in problems
    )


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


def test_unordered_mutation_of_one_region_is_rejected() -> None:
    """Reject two steps editing one `needs` list without an ordering

    Parallel PRs on the same aggregator list conflict, and the worse outcome is a
    rebase that silently drops the other's edit.
    """
    document = plan(
        "| **E4-4** | Extras | PR | — | S |",
        "| **E4-5** | Types | PR | — | S |",
    ) + "| `ci-required.needs` | E4-4, E4-5 |\n"
    steps, _ = checker.parse_plan(document)

    assert checker.check_mutation_order(document, steps) == [
        "E4-5 mutates ci-required.needs but does not depend on E4-4, so the two "
        "can land in parallel and drop each other's edit"
    ]


def test_ordered_mutation_of_one_region_is_accepted() -> None:
    """Accept a chain where each mutating step depends on the previous one"""
    document = plan(
        "| **E4-4** | Extras | PR | — | S |",
        "| **E4-5** | Types | PR | merge E4-4 | S |",
    ) + "| `ci-required.needs` | E4-4, E4-5 |\n"
    steps, _ = checker.parse_plan(document)

    assert checker.check_mutation_order(document, steps) == []


def test_mutation_order_accepts_a_transitive_chain() -> None:
    """Accept ordering established through an intermediate step"""
    document = plan(
        "| **E4-3** | Split | PR | — | M |",
        "| **E4-4** | Extras | PR | merge E4-3 | S |",
        "| **E4-5** | Types | PR | merge E4-4 | S |",
    ) + "| `ci-required.needs` | E4-3, E4-5 |\n"
    steps, _ = checker.parse_plan(document)

    assert checker.check_mutation_order(document, steps) == []


def test_mutation_region_listing_an_unknown_step_is_rejected() -> None:
    """Reject a contended-region row naming a step that does not exist"""
    document = plan("| **E4-4** | Extras | PR | — | S |") + (
        "| `ci-required.needs` | E4-4, E9-9 |\n"
    )
    steps, _ = checker.parse_plan(document)

    assert "ci-required.needs lists undefined step E9-9" in checker.check_mutation_order(
        document, steps
    )


def test_step_not_required_by_the_terminal_is_reported() -> None:
    """Reject a step the completion milestone does not transitively require

    "Everything is done" is worthless as a milestone if a whole epic can be added
    later without becoming its prerequisite.
    """
    rows = (
        "| **E9-7** | Proxy policy | PR | — | M |",
        "| **E13-1** | Suite complete | milestone | — | S |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    assert checker.check_terminal_reachability(steps, "E13-1") == [
        "E13-1 does not require E9-7; it could complete with that unfinished"
    ]


def test_terminal_reachability_follows_transitive_prerequisites() -> None:
    """Accept a step required only indirectly by the milestone"""
    rows = (
        "| **E9-4** | Chromium enforcement | PR | — | M |",
        "| **E9-6** | Chromium rebinding | PR | merge E9-4 | M |",
        "| **E13-1** | Suite complete | milestone | merge E9-6 | S |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    assert checker.check_terminal_reachability(steps, "E13-1") == []


def test_stale_declared_totals_are_reported() -> None:
    """Reject headline figures that no longer match the table

    A stale count is the one claim in the document a reader cannot check by
    reading, so the validator checks it instead.
    """
    document = plan("| **E0-1** | Deps | PR | — | S |") + (
        "<!-- totals: steps=99 authorable=7 pr_authorable=3 -->" + chr(10)
    )
    steps, _ = checker.parse_plan(document)

    problems = checker.check_declared_totals(document, steps, 0, 0)

    assert "declared steps=99 but the table has 1" in problems
    assert "declared authorable=7 but 0 steps are authorable after bootstrap" in problems


def test_missing_totals_declaration_is_reported() -> None:
    """Require the machine-readable totals comment to exist"""
    steps, _ = checker.parse_plan(plan("| **E0-1** | Deps | PR | — | S |"))

    assert checker.check_declared_totals("no comment here", steps, 0, 0) == [
        "no machine-readable totals declaration found; add "
        "'<!-- totals: steps=N authorable=M pr_authorable=K -->'"
    ]


def test_pr_authorability_is_reported_separately() -> None:
    """Count only PR steps as work an engineer can pick up

    Milestones and admin operations are authorable in the graph sense but are not
    work anyone can start, so counting them overstates capacity.
    """
    rows = (
        "| **E0-1** | Deps | PR | \u2014 | S |",
        "| **E1-6** | Green | milestone | \u2014 | S |",
        "| **E4-2** | Protection | operation | \u2014 | S |",
        "| **E5-1** | Parsers | PR | impl E0-1 | M |",
    )
    steps, _ = checker.parse_plan(plan(*rows))

    authorable = checker.authorable_after(steps, {"E0-1", "E0-2"})
    pr_only = {s for s in authorable if steps[s]["type"] == "PR"}

    assert authorable == {"E1-6", "E4-2", "E5-1"}
    assert pr_only == {"E5-1"}


def test_declared_pr_authorable_must_match() -> None:
    """Reject a stale staffing figure as well as a stale step count"""
    document = plan("| **E0-1** | Deps | PR | \u2014 | S |") + (
        "<!-- totals: steps=1 authorable=0 pr_authorable=9 -->" + chr(10)
    )
    steps, _ = checker.parse_plan(document)

    problems = checker.check_declared_totals(document, steps, 0, 0)

    assert problems == [
        "declared pr_authorable=9 but 0 PR steps are authorable after bootstrap"
    ]


def test_external_blocking_nothing_is_reported() -> None:
    """Reject an external prerequisite that gates no step on the path to done

    Otherwise a prerequisite can be added, block nothing, and be skipped while the
    completion milestone still passes.
    """
    document = plan(
        "| **E9-2** | Policy | PR | \u2014 | M |",
        "| **E13-1** | Suite complete | milestone | merge E9-2 | S |",
        externals="| **X-9** | Unused prerequisite | nothing | maintainer |" + chr(10),
    )
    steps, externals = checker.parse_plan(document)

    assert checker.check_external_completion(steps, externals, "E13-1") == [
        "external prerequisite X-9 blocks no step required by E13-1; it could be "
        "skipped while completion still passes"
    ]


def test_external_gating_a_required_step_is_accepted() -> None:
    """Accept an external that blocks a step the milestone requires"""
    document = plan(
        "| **E9-2** | Policy | PR | impl X-1 | M |",
        "| **E13-1** | Suite complete | milestone | merge E9-2 | S |",
        externals="| **X-1** | Outbound policy | E9-2 | maintainer |" + chr(10),
    )
    steps, externals = checker.parse_plan(document)

    assert checker.check_external_completion(steps, externals, "E13-1") == []


def test_real_plan_is_valid() -> None:
    """Keep the committed plan passing its own validator"""
    assert checker.main([str(checker.DEFAULT_PLAN)]) == 0
