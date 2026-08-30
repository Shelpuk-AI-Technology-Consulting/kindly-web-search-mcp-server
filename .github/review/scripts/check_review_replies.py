"""Fail when a review finding was closed with no answer anybody can read.

upstream, ported from an internal repository. **An open draft review silently absorbs every
thread reply written while it exists.** ``addPullRequestReviewThreadReply``
attaches a reply to the caller's *pending* review — a draft visible only to its
author — creating one if none exists. The mutation reports success,
``resolveReviewThread`` still works, and the thread then reads as answered to
whoever wrote it and as closed-in-silence to everybody else, including the
reviewer. On the sibling project's PR #45, seventeen replies landed that way and
the reviewer correctly raised the same finding on all eight of its rounds.

**The draft cannot be gated on directly.** A pending review is invisible to every
other token, and the REST review-comment endpoints omit it even from its author.
So this gates on the *symptom*: a thread marked resolved whose every comment has
one author.

🔴 **This gate is only meaningful because the reply convention changed with it.**
Measured upstream before that change: **404 review threads across 20
pull requests, every one single-author, none resolved.** Answers were written as
top-level pull request comments, so *resolved and single-author* was one keypress
away from being the ordinary shape rather than an anomaly — the gate would have
blocked a merge on 43 correctly-answered threads at once and told the author to
publish a reply that already existed somewhere else. ``CLAUDE.md`` now requires
the reply to be written **in the thread**, through the REST endpoint below, and
verified before the thread is resolved. Without that, this file is a nuisance;
with it, a resolved single-author thread genuinely means nobody can read the
answer.

**One place runs this today: the ``review_replies`` job in ``ci.yml``**, which
reports it as a check on the pull request so the state cannot be merged past.

⏸️ **Running it again as an early step inside ``claude-code-review.yml`` — so a
review that could only repeat itself is never paid for — is DEFERRED**, and the
reason is worth knowing before anyone adds it. That workflow's failure-notice
steps carry a bare ``if: steps.outcome.outputs.result != 'ok'``, which GitHub
evaluates as ``success() && …``, so a failing early step **skips** them — while
``Write run summary`` carries ``always()`` and defaults ``RESULT`` to ``fatal``.
A bare ``exit 1`` there would announce the workflow as misconfigured and post
nothing to the pull request: worse than the spend it was meant to save. It needs
a third run outcome that ``Resolve outcome`` produces and ``build_run_summary``
renders, which is its own change.

**The check is as of the last push.** Resolving a thread pushes no commit, so a
thread answered after the last push does not re-trigger it — which is why the
failure message ends by telling you to **re-run the job** rather than to push.
Getting that wrong is expensive: an empty commit re-triggers the billed review
this gate exists to protect.

Unlike :mod:`fetch_conversation`, an unreadable fetch **fails**. That module
supplies context, so degrading beats taking the review down with it. This is a
gate, and a gate that passes when it could not check is worse than no gate,
because it is believed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_conversation import _threads, thread_state  # noqa: E402

# Named once so the message, the tests and the documentation cannot drift.
REPLIES_ENDPOINT = "repos/{repo}/pulls/{pr}/comments/{comment_id}/replies"


def verdict(state: dict[str, int], fetch_ok: bool) -> tuple[int, str]:
    """Decide whether the pull request may proceed, and say why.

    Pure, so the decision can be tested without a network or a pull request.

    Args:
        state: Output of :func:`fetch_conversation.thread_state`.
        fetch_ok: Whether the thread list was actually read.

    Returns:
        ``(exit_code, message)``. Non-zero blocks.
    """

    if not fetch_ok:
        return 1, (
            "Could not read the review threads, so this gate could not run. Failing "
            "rather than passing: a check that reports success when it did not check "
            "is worse than no check, because it is believed. Re-run once the API is "
            "reachable."
        )

    unanswered = state.get("resolved_unanswered", 0)
    if not unanswered:
        total = state.get("total", 0)
        return 0, (
            f"{total} review thread(s); every resolved one carries a published answer."
        )

    return 1, (
        f"{unanswered} of {state.get('resolved', 0)} resolved review thread(s) carry no "
        "published answer — every comment in them is by one author.\n"
        "\n"
        "Two things produce this and the remedy differs, so do not assume the second.\n"
        "\n"
        "1. **Nobody answered.** If the thread genuinely needs no answer — your own "
        "question, a note on your own change — say so in it and resolve it. One "
        "sentence clears this.\n"
        "2. **You answered and it did not publish.** If you replied and the thread "
        "still looks like this, the reply is most likely sitting in a PENDING review: "
        "a draft only you can see. The GraphQL addPullRequestReviewThreadReply "
        "mutation puts it there, reports success, and says nothing. Check with:\n"
        "\n"
        '  gh api graphql -f query=\'{repository(owner:"OWNER",name:"NAME"){'
        "pullRequest(number:PR){reviews(first:100){nodes{state}}}}}' "
        "--jq '[.data.repository.pullRequest.reviews.nodes[]"
        '|select(.state=="PENDING")]|length\'\n'
        "\n"
        "Publish an existing draft with submitPullRequestReview(event:COMMENT). To "
        "reply in the first place, use the REST endpoint, which publishes immediately "
        "and notifies:\n"
        "\n"
        f"  gh api -X POST {REPLIES_ENDPOINT.format(repo='OWNER/NAME', pr='PR', comment_id='ID')} "
        "-f body='...'\n"
        "\n"
        "Then **re-run this job**. It reads the API live, so no commit is needed — and "
        "an empty commit would re-trigger the billed review this check exists to "
        "protect.\n"
        "\n"
        "If a thread genuinely needs no answer, say that in the thread and resolve it. "
        "Resolving is not answering, and a finding closed in silence is "
        "indistinguishable from one nobody read."
    )


def main() -> int:
    """Run the gate against one pull request.

    Returns:
        Process exit code: 0 to proceed, 1 to block.
    """

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, help="pull request number")
    args = parser.parse_args()

    threads, pull_author, ok = _threads(args.repo, args.pr)
    state = thread_state(threads, pull_author)
    code, message = verdict(state, ok)

    if code:
        # An `::error::` annotation renders on the pull request itself, which is
        # where somebody has to see it -- the state it reports is one the author
        # cannot see is wrong from their own screen.
        first, _, rest = message.partition("\n")
        print(f"::error::{first}")
        if rest.strip():
            print(rest)
    else:
        print(message)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
