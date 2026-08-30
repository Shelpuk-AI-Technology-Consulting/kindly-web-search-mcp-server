"""Validate the test suite implementation plan's dependency declarations.

The plan in ``.system_design/TEST_SUITE_IMPLEMENTATION_PLAN.md`` declares each
step's prerequisites in a table column, and separately draws a summary graph in
prose. The prose drawing is the part that drifts, so the per-step declarations are
the single source of truth and this script is what keeps them honest.

It deliberately rejects loose syntax rather than interpreting it. An earlier
version of this script accepted prose such as ``impl E1-1…E1-5`` and silently read
two of the five steps, and ignored ``X-*`` external prerequisites entirely, which
made its "startable now" count wrong in the optimistic direction. A validator that
guesses is worse than none, because it is believed.

Accepted grammar for the ``Blocked by`` cell::

    —                                   no prerequisites
    impl E0-1                           one prerequisite
    impl E0-1, merge E1-6, complete X-2  several, comma separated

Dependency kinds (validated against the target's declared Type):
    ``impl``: the artefact or decision is needed before authoring. Any target.
    ``merge``: authoring proceeds; a prerequisite PR must land first. PR targets only.
    ``complete``: authoring proceeds; a non-PR prerequisite must finish before this
        step merges or activates. Milestone, decision, operation or external only.

Exit codes:
    0: every declaration is well formed and the graph is acyclic.
    1: a syntax, reference, duplicate or cycle problem was found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: ``| **E0-1** | Title | PR | impl E0-2 | S |``
STEP_ROW = re.compile(
    r"^\|\s*\*\*(?P<id>E\d+-\d+[ab]?)\*\*\s*\|(?P<title>[^|]*)\|"
    r"(?P<type>[^|]*)\|(?P<deps>[^|]*)\|",
    re.M,
)
#: ``| **X-1** | Prerequisite | … ``
EXTERNAL_ROW = re.compile(r"^\|\s*\*\*(?P<id>X-\d+)\*\*\s*\|", re.M)
#: ``| `ci-required.needs` | E4-1, E4-3, E4-5 |`` in the contended-region table.
MUTATION_ROW = re.compile(
    r"^\|\s*`(?P<region>[a-z][\w.-]*)`\s*\|(?P<steps>[^|]*)\|", re.M
)
#: ``<!-- totals: steps=76 authorable=48 pr_authorable=44 -->``
TOTALS_DECLARATION = re.compile(
    r"<!--\s*totals:\s*steps=(?P<steps>\d+)\s+authorable=(?P<authorable>\d+)"
    r"\s+pr_authorable=(?P<pr_authorable>\d+)\s*-->"
)
#: ``Files: tests/a.py, tests/b.py``
FILES_LINE = re.compile(r"^\s*Files:\s*(?P<files>.+)$", re.M)
#: Framework use, not `unittest.mock`, which pytest-native tests use freely.
#: Matched at line start so the string quoted inside a test is not evidence.
UNITTEST_FRAMEWORK = re.compile(
    r"^import unittest\s*$|^from unittest import|unittest\.(TestCase|IsolatedAsyncioTestCase)",
    re.M,
)

DEPENDENCY = re.compile(r"^(?P<kind>impl|merge|complete)\s+(?P<id>E\d+-\d+[ab]?|X-\d+)$")
VALID_TYPES = {"PR", "milestone", "decision", "operation"}
NO_DEPS = {"—", "-", ""}

DEFAULT_PLAN = (
    Path(__file__).resolve().parents[1]
    / ".system_design"
    / "TEST_SUITE_IMPLEMENTATION_PLAN.md"
)


class PlanError(Exception):
    """Raised when the plan's declarations are malformed or inconsistent."""


def parse_dependencies(cell: str) -> list[tuple[str, str]]:
    """Parse one ``Blocked by`` cell into (kind, step id) pairs.

    Args:
        cell: The raw table cell, which may contain Markdown emphasis.

    Returns:
        The declared dependencies, empty when the cell records none.

    Raises:
        PlanError: If any entry does not match the accepted grammar. Ranges
            (``E1-1…E1-5``) and wildcards (``E5-*``) are rejected here rather
            than being partially understood.
    """
    text = cell.replace("**", "").replace("`", "").strip()
    if text in NO_DEPS:
        return []

    dependencies: list[tuple[str, str]] = []
    for entry in text.split(","):
        match = DEPENDENCY.match(entry.strip())
        if match is None:
            raise PlanError(
                f"unparseable dependency {entry.strip()!r} — expected "
                f"'impl|merge|complete <ID>'; ranges and wildcards are not allowed"
            )
        dependencies.append((match.group("kind"), match.group("id")))
    return dependencies


def parse_plan(text: str) -> tuple[dict[str, dict], set[str]]:
    """Extract every step and external prerequisite from the plan.

    Args:
        text: Full Markdown source of the implementation plan.

    Returns:
        A tuple of the step table (id to a record holding ``type`` and ``deps``)
        and the set of declared external prerequisite ids.

    Raises:
        PlanError: If a step id is declared twice, a step's ``Type`` is not one
            of the recognised values, or a dependency cell is malformed.
    """
    externals = {m.group("id") for m in EXTERNAL_ROW.finditer(text)}

    steps: dict[str, dict] = {}
    for match in STEP_ROW.finditer(text):
        step_id = match.group("id")
        if step_id in steps:
            raise PlanError(f"step {step_id} is declared more than once")

        step_type = match.group("type").replace("*", "").replace("`", "").strip()
        if step_type not in VALID_TYPES:
            raise PlanError(
                f"step {step_id} has Type {step_type!r}; "
                f"expected one of {sorted(VALID_TYPES)}"
            )

        steps[step_id] = {
            "type": step_type,
            "deps": parse_dependencies(match.group("deps")),
        }
    return steps, externals


def check_references(steps: dict[str, dict], externals: set[str]) -> list[str]:
    """Check every dependency resolves and uses a kind valid for its target.

    The kinds carry different scheduling meaning, so a mislabelled one silently
    changes what the plan claims can be parallelised:

    ``impl``
        The artefact or decision is needed before authoring. Valid for any target.
    ``merge``
        Authoring proceeds now; a prerequisite **PR** must land first. Only valid
        against a step of type ``PR`` -- a milestone or an admin operation has no
        merge to wait on.
    ``complete``
        Authoring proceeds now; a **non-PR** prerequisite must finish before this
        step merges or activates. Only valid against a milestone, decision,
        operation or external prerequisite.

    Args:
        steps: Parsed step table.
        externals: Declared external prerequisite ids.

    Returns:
        Human-readable problems, empty when every reference is valid.
    """
    known = set(steps) | externals
    problems = []
    for step_id, record in steps.items():
        for kind, dep in record["deps"]:
            if dep not in known:
                problems.append(f"{step_id} depends on undefined {dep}")
                continue
            target_type = "external" if dep in externals else steps[dep]["type"]
            if kind == "merge" and target_type != "PR":
                problems.append(
                    f"{step_id} declares 'merge {dep}', but {dep} is a "
                    f"{target_type} and has no PR to land; use 'complete'"
                )
            elif kind == "complete" and target_type == "PR":
                problems.append(
                    f"{step_id} declares 'complete {dep}', but {dep} is a PR; "
                    "use 'merge'"
                )
    return problems


def check_row_syntax(text: str, parsed: set[str]) -> list[str]:
    """Report table rows that look like steps but did not parse.

    A row missing a column silently fails :data:`STEP_ROW` and vanishes from the
    graph. That is the same class of defect as a mis-parsed dependency: the
    validator passes while the plan is wrong.

    Args:
        text: Full Markdown source of the implementation plan.
        parsed: Step ids that were successfully parsed.

    Returns:
        Human-readable problems, empty when every candidate row parsed.
    """
    candidates = set(re.findall(r"^\|\s*\*\*(E\d+-\d+[ab]?)\*\*", text, re.M))
    return [
        f"row for {step_id} looks like a step but did not parse — check its columns"
        for step_id in sorted(candidates - parsed)
    ]


def topological_order(steps: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Order the steps so every step follows its prerequisites.

    Args:
        steps: Parsed step table. External prerequisites are ignored, since they
            are roots by definition.

    Returns:
        A tuple of the ordered step ids and the ids that could not be ordered,
        the latter being those in or downstream of a cycle.
    """
    known = set(steps)
    blockers = {s: {d for _, d in r["deps"]} & known for s, r in steps.items()}
    remaining = dict(blockers)
    ordered: list[str] = []

    # Resolve in deterministic passes so the reported order is stable.
    while True:
        ready = sorted(s for s, b in remaining.items() if not b)
        if not ready:
            break
        for step in ready:
            ordered.append(step)
            del remaining[step]
        for b in remaining.values():
            b.difference_update(ready)

    return ordered, sorted(remaining)


def longest_chain(steps: dict[str, dict], ordered: list[str]) -> tuple[int, list[str]]:
    """Measure the longest prerequisite chain in the plan.

    Args:
        steps: Parsed step table.
        ordered: A valid topological ordering of those steps.

    Returns:
        The chain length in steps and one chain achieving it.
    """
    known = set(steps)
    depth: dict[str, int] = {}
    parent: dict[str, str | None] = {}

    # `ordered` guarantees every prerequisite is resolved before its dependent.
    for step in ordered:
        best, best_parent = 0, None
        for dep in {d for _, d in steps[step]["deps"]} & known:
            if depth[dep] + 1 > best:
                best, best_parent = depth[dep] + 1, dep
        depth[step], parent[step] = best, best_parent

    if not depth:
        return 0, []

    end = max(depth, key=lambda s: (depth[s], s))
    chain, node = [], end
    while node is not None:
        chain.append(node)
        node = parent[node]
    return depth[end] + 1, list(reversed(chain))


def check_mutation_order(text: str, steps: dict[str, dict]) -> list[str]:
    """Require steps mutating one contended region to be totally ordered.

    Two PRs editing the same ``needs`` list in parallel conflict, and the worse
    outcome is a silent one -- a rebase that drops a dependency added by the other.
    The plan promises these are chained; this makes the graph enforce it rather
    than the prose assert it.

    Args:
        text: Full Markdown source of the implementation plan.
        steps: Parsed step table.

    Returns:
        Human-readable problems, empty when each region's steps form a chain.
    """
    ancestors: dict[str, set[str]] = {}

    def resolve(step_id: str) -> set[str]:
        """Collect every step reachable backwards from ``step_id``."""
        if step_id in ancestors:
            return ancestors[step_id]
        ancestors[step_id] = set()
        found: set[str] = set()
        for _, dep in steps.get(step_id, {}).get("deps", []):
            if dep in steps:
                found.add(dep)
                found |= resolve(dep)
        ancestors[step_id] = found
        return found

    problems = []
    for match in MUTATION_ROW.finditer(text):
        region = match.group("region")
        listed = re.findall(r"E\d+-\d+[ab]?", match.group("steps"))
        unknown = [s for s in listed if s not in steps]
        problems += [f"{region} lists undefined step {s}" for s in unknown]

        known = [s for s in listed if s in steps]
        for earlier, later in zip(known, known[1:]):
            if earlier not in resolve(later):
                problems.append(
                    f"{later} mutates {region} but does not depend on {earlier}, "
                    "so the two can land in parallel and drop each other's edit"
                )
    return problems


def check_terminal_reachability(steps: dict[str, dict], terminal: str) -> list[str]:
    """Require every step to be a prerequisite of the completion milestone.

    A milestone whose acceptance says "everything is done" is worthless if a whole
    epic can be added later without becoming its prerequisite. This makes the claim
    structural: anything not reachable backwards from ``terminal`` is reported.

    Args:
        steps: Parsed step table.
        terminal: Id of the completion milestone.

    Returns:
        Human-readable problems, empty when every other step is reachable.
    """
    if terminal not in steps:
        return [f"completion milestone {terminal} is not declared"]

    reachable: set[str] = set()
    frontier = [terminal]
    while frontier:
        current = frontier.pop()
        for _, dep in steps.get(current, {}).get("deps", []):
            if dep in steps and dep not in reachable:
                reachable.add(dep)
                frontier.append(dep)

    return [
        f"{terminal} does not require {step}; it could complete with that unfinished"
        for step in sorted(set(steps) - reachable - {terminal})
    ]


def check_external_completion(
    steps: dict[str, dict], externals: set[str], terminal: str
) -> list[str]:
    """Require every external prerequisite to gate something that reaches the end.

    The completion milestone's acceptance says every external prerequisite is
    resolved. That is only structurally true if each one actually blocks a step on
    the path to it -- otherwise a prerequisite can be added to the table, block
    nothing, and be quietly skipped while completion still passes.

    Args:
        steps: Parsed step table.
        externals: Declared external prerequisite ids.
        terminal: Id of the completion milestone.

    Returns:
        Human-readable problems, empty when every external is load-bearing.
    """
    reachable: set[str] = set()
    frontier = [terminal]
    while frontier:
        current = frontier.pop()
        for _, dep in steps.get(current, {}).get("deps", []):
            if dep in steps and dep not in reachable:
                reachable.add(dep)
                frontier.append(dep)

    referenced = {
        dep
        for step_id in reachable | {terminal}
        for _, dep in steps.get(step_id, {}).get("deps", [])
    }
    return [
        f"external prerequisite {external} blocks no step required by {terminal}; "
        "it could be skipped while completion still passes"
        for external in sorted(externals - referenced)
    ]


def check_declared_totals(
    text: str, steps: dict[str, dict], startable: int, pr_startable: int
) -> list[str]:
    """Compare the plan's stated headline figures with the computed ones.

    The prose quotes a step count and an authorable count. Those go stale as soon
    as steps are added while the validator keeps passing, leaving the numbers a
    reader trusts as the only unchecked claims in the document.

    Args:
        text: Full Markdown source of the implementation plan.
        steps: Parsed step table.
        startable: Computed count of steps authorable after bootstrap.
        pr_startable: The same, restricted to steps of type ``PR``. This is the
            staffing figure: a milestone or an admin operation is not work an
            engineer can pick up, so counting them inflates capacity.

    Returns:
        Human-readable problems, empty when the declaration matches.
    """
    match = TOTALS_DECLARATION.search(text)
    if match is None:
        return [
            "no machine-readable totals declaration found; add "
            "'<!-- totals: steps=N authorable=M pr_authorable=K -->'"
        ]

    problems = []
    if int(match.group("steps")) != len(steps):
        problems.append(
            f"declared steps={match.group('steps')} but the table has {len(steps)}"
        )
    if int(match.group("authorable")) != startable:
        problems.append(
            f"declared authorable={match.group('authorable')} but {startable} "
            "steps are authorable after bootstrap"
        )
    if int(match.group("pr_authorable")) != pr_startable:
        problems.append(
            f"declared pr_authorable={match.group('pr_authorable')} but "
            f"{pr_startable} PR steps are authorable after bootstrap"
        )
    return problems


def authorable_after(steps: dict[str, dict], completed: set[str]) -> set[str]:
    """List the steps that can be *written* once ``completed`` has landed.

    Only unsatisfied ``impl`` dependencies stop authoring. A step waiting on a
    ``merge`` or ``complete`` prerequisite can be written today and simply cannot
    land yet -- which is the whole point of separating the kinds, and what an
    earlier version of this function ignored by treating every dependency alike.

    Args:
        steps: Parsed step table.
        completed: Ids treated as already delivered, including any resolved
            external prerequisites.

    Returns:
        The step ids that are authorable and not themselves already completed.
    """
    return {
        step_id
        for step_id, record in steps.items()
        if step_id not in completed
        and all(dep in completed for kind, dep in record["deps"] if kind == "impl")
    }


def check_file_ownership(text: str, repo_root: Path | None = None) -> list[str]:
    """Check the batched steps' file claims are unique, real and complete.

    Batched steps (the async migration) declare the files they own so two
    engineers cannot pick up the same file. Uniqueness alone is not enough: an
    empty, malformed or forgotten claim also leaves a file unowned, which is the
    failure this is actually guarding against.

    Args:
        text: Full Markdown source of the implementation plan.
        repo_root: Repository root, used to check the claimed paths exist and that
            every unittest-style test file is claimed. Existence and completeness
            checks are skipped when it is ``None`` or has no ``tests`` directory,
            so synthetic fragments can be validated in isolation.

    Returns:
        Human-readable problems, empty when every claim is unique, real and total.
    """
    claims: dict[str, int] = {}
    for match in FILES_LINE.finditer(text):
        entries = [p.replace("`", "").strip() for p in match.group("files").split(",")]
        entries = [e for e in entries if e]
        if not entries:
            return ["a Files: line declares no files"]
        for path in entries:
            claims[path] = claims.get(path, 0) + 1

    problems = [
        f"file {path} is claimed by {count} steps"
        for path, count in sorted(claims.items())
        if count > 1
    ]

    tests_dir = (repo_root / "tests") if repo_root else None
    if tests_dir is None or not tests_dir.is_dir():
        return problems

    problems += [
        f"claimed file {path} does not exist"
        for path in sorted(claims)
        if not (repo_root / path).exists()
    ]

    # Anything still on unittest needs an owning migration batch, or it is simply
    # forgotten -- the exact outcome the claim lists exist to prevent.
    unclaimed = sorted(
        f"tests/{path.name}"
        for path in tests_dir.glob("test_*.py")
        if UNITTEST_FRAMEWORK.search(path.read_text(encoding="utf-8"))
        and f"tests/{path.name}" not in claims
    )
    problems += [f"unittest-style {path} is claimed by no migration batch" for path in unclaimed]
    return problems


def main(argv: list[str] | None = None) -> int:
    """Validate the plan and report the outcome.

    Args:
        argv: Optional arguments; the first is a path to the plan file.

    Returns:
        A process exit code: 0 when the plan is valid, 1 otherwise.
    """
    args = sys.argv[1:] if argv is None else argv
    plan_path = Path(args[0]) if args else DEFAULT_PLAN

    try:
        steps, externals = parse_plan(plan_path.read_text(encoding="utf-8"))
    except PlanError as exc:
        print(f"FAIL: {exc}")
        return 1

    if not steps:
        print(f"FAIL: no steps parsed from {plan_path}")
        return 1

    source = plan_path.read_text(encoding="utf-8")
    problems = (
        check_references(steps, externals)
        + check_row_syntax(source, set(steps))
        + check_file_ownership(source, plan_path.resolve().parents[1])
        + check_mutation_order(source, steps)
        + check_terminal_reachability(steps, "E13-1")
        + check_external_completion(steps, externals, "E13-1")
    )

    ordered, stuck = topological_order(steps)
    if stuck:
        problems.append(f"cycle in or upstream of: {', '.join(stuck)}")

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1

    depth, chain = longest_chain(steps, ordered)
    startable = sorted(authorable_after(steps, {"E0-1", "E0-2"}))

    pr_startable = sorted(s for s in startable if steps[s]["type"] == "PR")
    stale = check_declared_totals(source, steps, len(startable), len(pr_startable))
    if stale:
        for problem in stale:
            print(f"FAIL: {problem}")
        return 1
    print(f"OK: {len(steps)} steps, {len(externals)} external prerequisites, acyclic")
    print(f"    longest chain: {depth} steps — {' -> '.join(chain)}")
    print(
        f"    authorable after E0-2: {len(startable)} "
        f"({len(pr_startable)} of them PR steps an engineer can pick up)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
