"""Render the pull request comment the review workflow keeps up to date.

Silence is the worst outcome for an automated review: someone reading the pull
request should never have to open the Actions tab to discover that nothing ran.
Both failure paths therefore leave a comment, and both name the project
administrator as the next step.

**Both failure paths fail the check.** There is one provider and no fallback, so
either way the pull request was not reviewed and a green check would claim it
had been. They still differ in who fixes it, which is the whole reason the
classification survives:

* **exhausted** — the provider is out of quota, its credentials were rejected,
  or the call failed transiently. The change is fine; top up or wait, then
  re-run.
* **fatal** — the workflow itself is misconfigured. Somebody has to edit it.
  ⚠️ Not *"and re-running will not clear it"*, which this said until upstream and
  which the workflow could not then know. 🔴 **upstream made it knowable and
  retired the stopgap this line used to point at.** Between the two tickets, all
  three operator surfaces carried a hand-written paragraph telling a reader to
  check the run's duration themselves, because a provider that hangs and a
  workflow that is misconfigured produced the same empty execution record.
  ``interpret_claude_result.classify`` now reads that duration and routes a
  timed-out attempt to **exhausted**, so this branch no longer covers it and the
  paragraph would be advice about a case that cannot arrive here.

**And a third outcome, which is the only one that is good news.**

* **superseded** — a later run reviewed the pull request successfully, so an
  existing notice is now wrong. :func:`build_superseded` renders the replacement.
  🔴 There was no such path until upstream: three steps carried
  ``if: result != 'ok'`` and nothing on the ``ok`` path touched the comment, so
  one transient marked a pull request unreviewed permanently while its own
  closing line promised it replaced itself on every run.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = "<!-- claude-code-review-notice -->"

#: The closing line every notice carries, in one place because upstream is what
#: made it TRUE. It read as a promise -- and the promise is what stopped a reader
#: suspecting a stale banner -- while being true only of a failing run replacing
#: a failing run. A success never touched the comment. Two renderers restating it
#: could drift apart again, and the drift would be invisible.
CLOSING_LINE = (
    "<sub>This notice replaces itself on each run, so there is only ever one.</sub>"
)

# Best effort, and against the currently configured endpoint it usually finds
# nothing: OpenRouter reports a spent credit balance without a reset time,
# because a topped-up balance has no schedule. The wording matched here is a
# coding-plan phrasing observed upstream. Kept because the extraction is guarded
# by `if reset:` and costs nothing, and because switching the kitty profile to
# a provider that does report one should not require re-adding the parsing.
RESET_PATTERN = re.compile(
    r"limit will reset at ([0-9]{4}-[0-9]{2}-[0-9]{2}[^\]\"\n]*)", re.I
)

#: Characters of diagnostic the comment carries. A pull request comment holds far
#: more, but the diagnostic is a debugging aid inside a collapsed block, not the
#: notice -- a reader who needs all of it has `artifacts/`.
DIAGNOSTIC_BUDGET = 4000

#: How ``interpret_claude_result._write_diagnostic`` opens each attempt's section.
SECTION_MARKER = "=== tier "


def _split_sections(text: str) -> list[str]:
    """Split an accumulated diagnostic into one part per attempt.

    Args:
        text: The whole diagnostic file's contents.

    Returns:
        One string per attempt, in order. Anything written before the first
        marker becomes the leading part rather than being dropped.
    """

    parts: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith(SECTION_MARKER) and current:
            parts.append("\n".join(current).strip("\n"))
            current = []
        current.append(line)
    if current:
        parts.append("\n".join(current).strip("\n"))
    return [part for part in parts if part]


def _bound_section(section: str, budget: int) -> str:
    """Tail-bound one section while keeping the header that identifies it.

    A plain tail slice removes the ``=== tier ... ===`` line, which is the only
    thing saying which attempt the text below belongs to.

    Args:
        section: One attempt's diagnostic text.
        budget: Characters this section may occupy.

    Returns:
        The section, at most ``budget`` characters long.
    """

    if len(section) <= budget:
        return section
    header, _, rest = section.partition("\n")
    room = budget - len(header) - 1
    if room <= 0:
        return header[:budget]
    return header + "\n" + rest[-room:]


def embed_diagnostic(diagnostic: str, budget: int = DIAGNOSTIC_BUDGET) -> str:
    """Reduce the diagnostic to what the comment can carry, fairly across attempts.

    🔴 **This was a single tail slice, and the half it cut was the half that
    mattered (upstream).** ``_write_diagnostic`` APPENDS a section per attempt, and
    two sections routinely exceed the budget — so on a retried failure the
    comment showed attempt 2 and dropped attempt 1, which is the one that says
    *why* a retry was needed. Both were complete in ``artifacts/``, and nobody
    opens ``artifacts/`` to read a comment they can already see.

    Budgeting per section rather than raising the cap: the number of attempts is
    bounded at two, but the size of one section is not — a bridge stack trace
    and a 60-line record tail are both in there.

    Args:
        diagnostic: The accumulated diagnostic text.
        budget: Total characters the embedded block may occupy.

    Returns:
        The text to embed, at most ``budget`` characters, empty when there is
        nothing to say.
    """

    text = diagnostic.strip()
    if not text:
        return ""

    parts = _split_sections(text)
    # One section, or none the marker recognises: the old behaviour is correct,
    # and is what an unsectioned diagnostic from any future writer still gets.
    if len(parts) <= 1:
        return text[-budget:]

    # `- (len(parts) - 1)` pays for the newlines the join puts back, so the
    # result is provably within budget rather than within budget plus slack.
    share = (budget - (len(parts) - 1)) // len(parts)
    if share <= 0:
        return text[-budget:]
    return "\n".join(_bound_section(part, share) for part in parts)


def extract_reset_time(diagnostic: str) -> str | None:
    """Find a provider-supplied quota reset time in the diagnostic.

    Args:
        diagnostic: Accumulated diagnostic text from the attempt.

    Returns:
        The reset timestamp as written by the provider, or None when absent.
    """

    match = RESET_PATTERN.search(diagnostic or "")
    return match.group(1).strip() if match else None


def _attempt_line(attempts: int | None, outcome: str) -> str:
    """State how many times the review was tried.

    Rendered on every notice, including the one-attempt case. Mentioning
    attempts only when there were two would make the common notice read as
    though nothing had been tried, and leave a reader unable to tell an
    automatic retry from a workflow that never retries.

    🔴 **None is a distinct answer, and it exists because the alternative was a
    lie.** This took ``attempts: int = 1`` and the workflow passed
    ``"${ATTEMPTS:-1}"``, so a dropped environment binding rendered *"1 attempt
    was made. It was not retried"* on a run that **had** retried — the exact
    opposite of the truth, in the notice a reader trusts most. A default that
    is wrong in a specific, confident direction is worse than an admission of
    not knowing.

    Args:
        attempts: How many review attempts the run made, or None when the run
            did not record it.
        outcome: The resolved outcome, ``exhausted`` or ``fatal``. Read only for
            the single-attempt case, where upstream made the two diverge: an
            unretried ``exhausted`` is a cost decision and an unretried
            ``fatal`` is a futility one, and saying the second about the first
            is the defect that ticket is named for.

    Returns:
        A Markdown line.
    """

    if attempts is None:
        return (
            "**The number of attempts was not recorded** -- the workflow did "
            "not pass one through. Read the diagnostic below: it carries one "
            "`=== tier ... ===` section per attempt."
        )
    if attempts >= 2:
        return (
            f"**{attempts} attempts** were made -- the first failed and the run "
            "retried it automatically, without waiting for anybody. Both are in "
            "the diagnostic below."
        )
    if attempts == 1 and outcome == "exhausted":
        # 🔴 upstream created this state and it had no wording of its own.
        # Before it, every `exhausted` was retried automatically, so `exhausted`
        # with one attempt could not occur -- and the `fatal` sentence below,
        # which says a re-run "could not have helped", was the only one a single
        # attempt could render. upstream routes a timed-out attempt to
        # `exhausted` while keeping upstream's refusal to spend a second one on
        # it, so the ticket filed about a message that wrongly declares a re-run
        # futile would have published exactly that message on its own case.
        #
        # ⚠️ The distinction is cost, not futility, and the sentence has to say
        # which: the attempt was billed for the whole budget it burned, so the
        # run declines to spend again on a reader's behalf rather than declining
        # to believe a re-run works. It does work -- that is what this notice is
        # asking for.
        return (
            "**1 attempt** was made, and the run did not retry it "
            "automatically -- not because a re-run cannot help, but because "
            "this attempt ran long enough to have been billed in full, and a "
            "second one costs the same again. Re-running by hand is the right "
            "next step."
        )
    if attempts == 1:
        return (
            "**1 attempt** was made. It was not retried: re-running it "
            "automatically could not have helped."
        )
    return "**No attempt was made** -- the run failed before the review started."


def build_superseded(review_run_url: str | None = None) -> str:
    """Render the note that replaces a notice a later run has disproved.

    🔴 **This exists because the closing line was false in the one direction
    that mattered (upstream).** Every notice ends *"This notice replaces itself on
    each run, so there is only ever one"* — true of a failing run replacing a
    failing run, false of a success, which never touched the comment at all. One
    transient therefore left *"Automatic code review failed … please ask the
    project administrator for help"* as the most prominent comment on a pull
    request the reviewer had since read five times, and the closing line is
    exactly what stopped a reader suspecting the banner was stale.

    **Replacing rather than deleting, deliberately.** The transient that produces
    these notices is unexplained and out of scope, and this comment is the only
    surface where somebody would notice a pattern in it; replacing is reversible
    where deleting is not; and it keeps the copy here, where it is testable. A
    deletion would live entirely in `github-script` JavaScript, which nothing
    here can test: the runners carry no `node` on ``PATH`` for a ``run:`` step
    and this repository has no JavaScript test harness. *(The action itself runs
    fine, on node bundled inside it — the limit is the harness, not the
    runtime.)*

    ⚠️ **Every later successful run rewrites this note**, because it matches the
    same marker. That is intended, not a bug: it refreshes the run link, and the
    superseded failure text simply moves one revision further down the comment's
    edit history rather than being lost.

    ⚠️ **The caller must only ever UPDATE with this, never create.** Creating it
    where no notice exists would put "an earlier run failed" on every clean pull
    request in the repository: louder and more misleading than the bug it fixes,
    and indistinguishable from the fix working. The workflow step is held to that
    by a wiring test.

    ⚠️ **What this does not fix**, so it is not mistaken for a win: on a
    fail → succeed → fail sequence the third run updates this same comment in
    place, at its original position in the timeline, where a reader can miss it.
    That is the behaviour a fail → fail sequence already had, so nothing
    regresses — but nothing improves there either.

    Args:
        review_run_url: Link to the run that produced the review, or None/empty
            when the caller passed none. Empty renders no link rather than a bare
            one: an unset ``${{ }}`` interpolates to the empty string rather than
            erroring, which is how upstream's attempt count went wrong.

    Returns:
        The comment body, beginning with the same marker the failure notices use.
    """

    lines = [
        MARKER,
        "## Automatic code review: earlier failure notice superseded",
        "",
        "An earlier run of this workflow failed on this pull request and posted "
        "a failure notice here. **A later run succeeded, so this pull request "
        "HAS been reviewed** — the review is on this page.",
        "",
        "Nothing needs fixing and nobody needs asking: the earlier failure was "
        "in the review run, not in this change, and it did not survive. The "
        "failed run stays in this workflow's run history, and the notice it "
        "posted — its reason, its attempt count and its diagnostic — stays in "
        "this comment's **edit history**.",
    ]
    if review_run_url:
        lines += ["", f"The run that produced the review: {review_run_url}"]
    lines += ["", CLOSING_LINE]

    return "\n".join(lines)


def build(
    outcome: str, tiers: list[str], diagnostic: str, *, attempts: int | None = None
) -> str:
    """Build the Markdown comment body.

    Args:
        outcome: Either ``"exhausted"`` or ``"fatal"``.
        tiers: Human-readable names of the providers that were attempted.
        diagnostic: Accumulated diagnostic text, embedded in a collapsed block.
        attempts: How many review attempts the run made, or None when the caller
            does not know. The default is None rather than 1 so a caller that
            forgets says so instead of asserting a number — see
            :func:`_attempt_line`. The workflow is separately held to passing it
            by a wiring test.

    Returns:
        The comment body, beginning with the marker used to update in place.
    """

    attempted = "\n".join(f"- {name}" for name in tiers) or "- (none reached)"

    if outcome == "exhausted":
        lines = [
            MARKER,
            "## Automatic code review did not run",
            "",
            "The model provider was unavailable, so this pull request has not "
            "been reviewed automatically and **the `review` check has failed**.",
            "",
            "**Providers attempted**",
            attempted,
            "",
            _attempt_line(attempts, outcome),
        ]
        reset = extract_reset_time(diagnostic)
        if reset:
            lines += ["", f"The provider reports its quota resets at **{reset}**."]
        lines += [
            "",
            "**There is nothing wrong with this change.** The check fails "
            "because an unreviewed pull request should not look like a reviewed "
            "one — not because the change is at fault. Re-run the *Claude Code "
            "Review* workflow once capacity is back, or push a new commit to "
            "trigger it.",
            "",
            "If this keeps happening, the provider quota needs topping up — "
            "please ask the project administrator for help.",
        ]
    elif outcome == "fatal":
        lines = [
            MARKER,
            "## Automatic code review failed",
            "",
            "The review workflow could not run to completion. This is a problem "
            "with the **workflow configuration**, not with the changes in this "
            "pull request.",
            "",
            "**Providers attempted**",
            attempted,
            "",
            _attempt_line(attempts, outcome),
            "",
            "**If a re-run fails the same way, please ask the project "
            "administrator for help.** This needs a fix in the workflow before "
            "automatic review works again.",
        ]
    else:
        # 🔴 This was a bare `else`, so ANY unrecognised outcome rendered the
        # `fatal` notice -- "Automatic code review failed", with a diagnostic and
        # an administrator to go and ask. upstream nearly routed the SUCCESS note
        # through this function, where one word's drift would have published that
        # heading on the run that proves the opposite. A wrong-but-plausible body
        # from a typo is worse than a crash: the crash is visible.
        raise ValueError(
            f"unknown outcome {outcome!r}: this renders a FAILURE notice, and the "
            "vocabulary is deliberately closed to 'exhausted' and 'fatal'. A "
            "success is not an outcome here -- see `build_superseded`."
        )

    embedded = embed_diagnostic(diagnostic)
    if embedded:
        lines += [
            "",
            "<details><summary>Diagnostic</summary>",
            "",
            "```",
            embedded,
            "```",
            "",
            "</details>",
        ]

    lines += ["", CLOSING_LINE]

    return "\n".join(lines)


def main() -> int:
    """Render the notice to a file.

    Returns:
        0 on success, 1 when the outcome argument is not recognised.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    # ⚠️ `--outcome` is fed `${{ steps.outcome.outputs.result }}`, whose alphabet
    # is `ok | exhausted | fatal`. A notice KIND is not a run outcome, so the
    # superseded note gets its own flag rather than a third choice here: that
    # keeps the failure vocabulary closed, and `build()` now raises on anything
    # outside it instead of falling through to "Automatic code review failed".
    parser.add_argument("--outcome", choices=["exhausted", "fatal"])
    parser.add_argument(
        "--superseded",
        action="store_true",
        help=(
            "Render the note that retracts a failure notice a later run has "
            "disproved. Mutually exclusive with --outcome."
        ),
    )
    parser.add_argument(
        "--tiers",
        default="",
        help="Comma-separated provider names that were attempted.",
    )
    parser.add_argument("--diagnostic", help="Path to the accumulated diagnostic file.")
    parser.add_argument(
        "--attempts",
        default="",
        help=(
            "How many review attempts the run made (upstream). Anything that is "
            "not a whole number -- including the empty string a dropped "
            "workflow binding produces -- renders as 'not recorded' rather "
            "than failing the step or inventing a count."
        ),
    )
    parser.add_argument(
        "--review-run-url",
        default="",
        help=(
            "With `--superseded`, a link to the run that produced the "
            "review. Empty renders no link rather than a bare one -- an unset "
            "workflow interpolation is the empty string, not an error."
        ),
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.superseded == bool(args.outcome):
        parser.error(
            "give exactly one of --outcome {exhausted,fatal} or --superseded; "
            "neither renders nothing and both is a contradiction"
        )

    diagnostic = ""
    if args.diagnostic and Path(args.diagnostic).is_file():
        diagnostic = Path(args.diagnostic).read_text(encoding="utf-8", errors="replace")

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    raw = args.attempts.strip()
    attempts = int(raw) if raw.isdigit() else None
    # upstream. The success path renders no diagnostic, no tiers and no attempt
    # count: it is not reporting a failure, it is retracting one.
    if args.superseded:
        body = build_superseded(review_run_url=args.review_run_url.strip())
    else:
        body = build(args.outcome, tiers, diagnostic, attempts=attempts)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    kind = "superseded" if args.superseded else args.outcome
    print(f"notice written: {kind}, tiers={len(tiers)}, {len(body)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
