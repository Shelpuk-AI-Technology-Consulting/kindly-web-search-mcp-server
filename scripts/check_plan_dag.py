"""Validate the test suite implementation plan's dependency graph.

The plan in ``.system_design/TEST_SUITE_IMPLEMENTATION_PLAN.md`` declares each
step's prerequisites in a table column, and separately draws a summary graph in
prose. The prose drawing is the part that drifts, and an earlier revision of the
plan shipped three dependency cycles that a reader had to find by hand.

This script treats the per-step ``Blocked by`` declarations as the single source
of truth: it parses them, checks every referenced step exists, and fails if the
resulting graph contains a cycle. Run it whenever the plan changes.

Exit codes:
    0: the graph is complete and acyclic.
    1: an undefined step is referenced, or a cycle exists.
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

STEP_ROW = re.compile(r"^\|\s*\*\*(E\d+-\d+[ab]?)\*\*\s*\|([^|]*)\|([^|]*)\|", re.M)
STEP_ID = re.compile(r"E\d+-\d+[ab]?")

DEFAULT_PLAN = (
    Path(__file__).resolve().parents[1]
    / ".system_design"
    / "TEST_SUITE_IMPLEMENTATION_PLAN.md"
)


def parse_steps(text: str) -> dict[str, set[str]]:
    """Extract each step's declared prerequisites from the plan's tables.

    Args:
        text: Full Markdown source of the implementation plan.

    Returns:
        A mapping of step id to the set of step ids it declares as blockers.
        External prerequisites (``X-*``) are tracked in the plan's prose rather
        than here, so they are deliberately not returned.
    """
    steps: dict[str, set[str]] = {}
    for match in STEP_ROW.finditer(text):
        step_id, blocked_by = match.group(1), match.group(3)
        steps.setdefault(step_id, set()).update(
            set(STEP_ID.findall(blocked_by)) - {step_id}
        )
    return steps


def find_cycle(steps: dict[str, set[str]]) -> list[str]:
    """Report the steps that cannot be topologically ordered.

    Args:
        steps: Mapping of step id to its declared blockers.

    Returns:
        The sorted ids participating in a cycle, or an empty list when the graph
        is acyclic.
    """
    known = set(steps)
    indegree = {step: len(blockers & known) for step, blockers in steps.items()}
    queue = [step for step, count in indegree.items() if count == 0]
    ordered: list[str] = []

    # Standard Kahn's algorithm; anything left unordered is in or behind a cycle.
    while queue:
        step = queue.pop()
        ordered.append(step)
        for candidate, blockers in steps.items():
            if step in blockers:
                indegree[candidate] -= 1
                if indegree[candidate] == 0:
                    queue.append(candidate)

    return sorted(known - set(ordered))


def longest_chain(steps: dict[str, set[str]]) -> int:
    """Measure the longest dependency chain, as a step count.

    Args:
        steps: Mapping of step id to its declared blockers. Must be acyclic.

    Returns:
        The number of steps in the longest chain, minimum 1 for a non-empty plan.
    """
    known = set(steps)
    depth: dict[str, int] = collections.defaultdict(int)
    for step in sorted(steps, key=lambda s: len(steps[s])):
        for blocker in steps[step] & known:
            depth[step] = max(depth[step], depth[blocker] + 1)
    return (max(depth.values()) + 1) if depth else len(known)


def main(argv: list[str] | None = None) -> int:
    """Validate the plan and report the outcome.

    Args:
        argv: Optional command-line arguments; the first is a path to the plan.

    Returns:
        A process exit code: 0 when the graph is valid, 1 otherwise.
    """
    args = sys.argv[1:] if argv is None else argv
    plan_path = Path(args[0]) if args else DEFAULT_PLAN
    steps = parse_steps(plan_path.read_text(encoding="utf-8"))

    if not steps:
        print(f"no steps parsed from {plan_path}", file=sys.stderr)
        return 1

    failed = False

    # An undefined reference usually means a step was renamed but not everywhere.
    undefined = sorted({d for blockers in steps.values() for d in blockers} - set(steps))
    if undefined:
        print(f"FAIL: references to undefined steps: {', '.join(undefined)}")
        failed = True

    cycle = find_cycle(steps)
    if cycle:
        print(f"FAIL: dependency cycle involving: {', '.join(cycle)}")
        failed = True

    if failed:
        return 1

    bootstrap = {"E0-1", "E0-2"}
    ready = sorted(s for s in steps if steps[s] <= bootstrap and s not in bootstrap)
    print(f"OK: {len(steps)} steps, acyclic, longest chain {longest_chain(steps)}")
    print(f"    startable immediately after E0-2: {len(ready)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
