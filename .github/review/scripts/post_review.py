"""Turn review findings into a GitHub pull request review payload.

Reads the structured findings produced by Claude and emits two artifacts: the
review payload consumed by ``pulls.createReview`` and the summary comment body.
Keeping this deterministic — rather than letting the model call the GitHub API
itself — means re-pushes update one comment instead of stacking duplicates, and
suggestion formatting is enforced rather than hoped for.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import findings_schema

MARKER = "<!-- claude-code-review -->"

# 🔴 upstream. The caps in `review_findings.schema.json` used to reach the CLI,
# where they were compiled into an ajv validator that re-prompts the model on
# any mismatch -- all-or-nothing over the whole document, so one over-long
# `title` cost the summary and every finding, and a run that could not satisfy
# them returned NO REVIEW at all after being billed in full.
#
# They are stripped from what the CLI sees (`findings_schema.strip_for_cli`) and
# enforced here instead, on the way out, where over-running one costs a
# truncated string. The numbers are READ FROM THE SCHEMA FILE, never restated:
# it stays the single place a cap is written down.
TRUNCATION_MARK = " [truncated]"


def _truncate(value: object, cap: int | None) -> tuple[object, bool]:
    """Cut an over-long string to its declared cap, visibly.

    Args:
        value: Candidate field value; anything but a string is returned as is.
        cap: Declared maximum length, or None to leave the value alone.

    Returns:
        A ``(value, was_truncated)`` pair. The marker is included **within** the
        cap rather than appended past it, so the result never exceeds the number
        the schema declares — including when the cap is too small to hold the
        marker at all, where the value is simply cut. Every cap in the schema
        today is comfortably larger than the marker, but a promise that holds
        only for the current numbers is not a promise.
    """

    if not isinstance(value, str) or not cap or len(value) <= cap:
        return value, False
    # A cap at or below the marker's own length cannot hold it, and appending it
    # anyway returns something LONGER than the cap -- the one direction a
    # truncation must never fail in.
    if cap <= len(TRUNCATION_MARK):
        return value[:cap], True
    return value[: cap - len(TRUNCATION_MARK)] + TRUNCATION_MARK, True


def _apply_caps(data: dict, caps: dict) -> tuple[dict, dict]:
    """Bring a findings document within the caps the schema declares.

    Four different things happen, and the differences are deliberate:

    * **Strings are truncated.** Losing the tail of a rationale costs a reader a
      sentence.
    * **``suggested_code`` is DROPPED, never truncated.** It is rendered inside
      a ``suggestion`` fence, which GitHub turns into a one-click apply button --
      so a truncated value is not a shortened note, it is a **corrupt patch**
      that replaces real source with half a statement plus the literal text
      ``[truncated]``. Its cap was more than halved by upstream and stopped being
      enforced at the CLI, so over-running it is the expected case rather than a
      corner. The schema's own description already says a suggestion that long
      *"is a rewrite rather than a fix: describe it in the rationale instead"*.
      The finding survives; only the button goes.
    * **``other_instances`` is trimmed**, and the trim is disclosed on the
      comment. The ticket asks for the twenty-first entry to go; a silently
      shortened list would read as the complete one.
    * **Findings are never dropped.** An over-long ``findings`` array is
      reported in full and the overflow disclosed. Discarding a real finding to
      honour a cap that no longer gates anything would make this fix cost more
      than the bug did, and the repository's standing rule on this path is to
      err toward reporting MORE.

    Args:
        data: Decoded findings document.
        caps: Output of :func:`findings_schema.caps`, or ``{}`` to do nothing.

    Returns:
        A ``(data, report)`` pair. ``data`` is a corrected copy; ``report``
        counts what changed, for disclosure in the summary.
    """

    report = {"strings": 0, "instances": 0, "overflow": 0, "suggestions": 0}
    if not caps:
        return data, report

    data = json.loads(json.dumps(data))

    for name, cap in (caps.get("document") or {}).items():
        value, cut = _truncate(data.get(name), cap)
        if cut:
            data[name] = value
            report["strings"] += 1

    text_caps = caps.get("finding_text") or {}
    instances_max = caps.get("other_instances_max")
    instance_max = caps.get("other_instance_max")

    findings = [f for f in data.get("findings", []) if isinstance(f, dict)]
    for finding in findings:
        # Handled before the truncation loop below, and excluded from it: a
        # truncated suggestion is an applyable corrupt patch, not a shortened
        # note. See the docstring.
        suggestion_cap = text_caps.get("suggested_code")
        suggested = finding.get("suggested_code")
        if (
            suggestion_cap
            and isinstance(suggested, str)
            and len(suggested) > suggestion_cap
        ):
            finding["suggested_code"] = None
            finding["_dropped_suggestion"] = len(suggested)
            report["suggestions"] += 1

        for name, cap in text_caps.items():
            if name == "suggested_code":
                continue
            value, cut = _truncate(finding.get(name), cap)
            if cut:
                finding[name] = value
                report["strings"] += 1

        instances = finding.get("other_instances")
        if not isinstance(instances, list):
            continue
        trimmed = [
            _truncate(item, instance_max)[0]
            for item in instances
            if isinstance(item, str)
        ]
        if instances_max and len(trimmed) > instances_max:
            # Recorded on the finding so it survives sorting and the severity
            # floor, and is rendered next to the list it belongs to.
            finding["_dropped_instances"] = len(trimmed) - instances_max
            trimmed = trimmed[:instances_max]
            report["instances"] += 1
        finding["other_instances"] = trimmed

    findings_max = caps.get("findings_max")
    if findings_max and len(findings) > findings_max:
        report["overflow"] = len(findings) - findings_max

    return data, report


# From this round on, only `warning` and `critical` are reported.
#
# Measured upstream: #208 ran 18 reviewer rounds and #213 ran 13, both
# ending because the author stopped pushing rather than because the reviewer ran
# out of things to say. From round 6 onward those two carried 40 findings --
# **0 critical, 7 warning, 33 suggestion** -- so this removes 82.5% of the late
# traffic and would have kept every warning **on those two pull requests**. That
# is what the floor did to the data we have, not a guarantee about a
# distribution nobody has measured.
#
# 🔴 **A floor, not a round cap, and the difference is load-bearing here.** Five
# of those seven late warnings are distinct, among them an ownership load
# sitting outside its error handler and a prompt-injection amplifier. A cap
# discards them; a floor does not. This is where our data differs from the
# project this was ported from, whose late rounds carried no warnings at all --
# so the cheaper change would have been defensible there and is not here.
#
# ⚠️ Enforced HERE rather than by instructing the model. The only per-run channel
# to the model is the conversation span, which is fenced as untrusted with an
# explicit rule that nothing inside may relax a rule -- and a severity floor IS a
# relaxation, so a compliant model would have to ignore it. Worse, `_defuse`
# cannot distinguish our sentence from a comment reproducing it, so anyone able
# to comment could have posted a floor notice and silenced a review that has
# raised zero criticals in 82 findings.
FLOOR_FROM_ROUND = 6

# Severities that survive the floor.
ABOVE_FLOOR = frozenset({"critical", "warning"})

SEVERITY_ORDER = {"critical": 0, "warning": 1, "suggestion": 2}
# Ties within a severity break toward the finding a reader can act on without
# checking it first. An absent value sorts with high rather than low: a payload
# predating the field is not a statement of doubt, and treating it as one would
# bury older findings under newer hedged ones.
CONFIDENCE_ORDER = {"high": 0, "": 0, "medium": 1, "low": 2}
SEVERITY_LABEL = {
    "critical": "Critical",
    "warning": "Warning",
    "suggestion": "Suggestion",
}


def _finding_body(finding: dict) -> str:
    """Render one finding as an inline review comment body.

    When the finding carries replacement source, it is emitted as a fenced
    ``suggestion`` block, which GitHub renders as a one-click applyable fix.
    When it carries ``other_instances``, they are listed under **Also at:** so a
    defect occurring in several places costs one round rather than one per site.

    Args:
        finding: A single validated finding object.

    Returns:
        Markdown body for the inline comment.
    """

    severity = str(finding.get("severity", "suggestion")).lower()
    label = SEVERITY_LABEL.get(severity, "Suggestion")
    category = str(finding.get("category") or "").strip()

    header = f"**{label}** · `{category}`" if category else f"**{label}**"
    parts = [
        header,
        "",
        f"**{finding.get('title', '').strip()}**",
        "",
        str(finding.get("rationale", "")).strip(),
    ]

    suggested = finding.get("suggested_code")
    if isinstance(suggested, str) and suggested.strip():
        # Trailing newline is stripped: GitHub adds one, and a doubled newline
        # makes the applied suggestion insert a blank line.
        parts += ["", "```suggestion", suggested.rstrip("\n"), "```"]

    # A dropped suggestion is disclosed rather than silently absent -- otherwise
    # the finding reads as one the reviewer had no concrete fix for, which is a
    # different and more discouraging thing than one whose fix was too long to
    # offer as a button.
    dropped_suggestion = finding.get("_dropped_suggestion")
    if isinstance(dropped_suggestion, int) and dropped_suggestion > 0:
        parts += [
            "",
            # ⚠️ Points at the artifact, and does NOT claim the rationale
            # describes the fix. The schema tells the model to supply
            # `suggested_code` OR prose, so a model that sent 1,300 characters
            # of replacement did so INSTEAD of describing it -- the rationale may
            # say nothing about it. The full text survives in
            # `artifacts/review_findings.json`, which is written before capping.
            f"<sub>A replacement of {dropped_suggestion} characters was offered "
            "and is not shown: at that length it is a rewrite rather than an "
            "applyable fix. The full text is in the run's `review_findings.json` "
            "artifact.</sub>",
        ]

    # One defect, reported once, with every place it occurs. Measured on #208: a
    # single wrong endpoint count was reported seven times across four documents,
    # one site per round -- each round the author fixed what was named and the
    # next round named the next site.
    #
    # Guarded by type rather than trusted: the schema is enforced provider-side
    # only (this module does `json.loads` and builds; `jsonschema` is absent from
    # the bare interpreter CI uses), so a bare string can arrive where a list
    # belongs, and iterating it would render one bullet per character.
    instances = finding.get("other_instances")
    if isinstance(instances, list):
        listed = [
            item.strip() for item in instances if isinstance(item, str) and item.strip()
        ]
        if listed:
            parts += ["", "**Also at:**"] + [f"- `{item}`" for item in listed]
            # No silent caps. A shortened list reads as the complete one, and
            # this list exists precisely so a defect costs one round instead of
            # five -- so a reader has to be told when it was cut.
            dropped = finding.get("_dropped_instances")
            if isinstance(dropped, int) and dropped > 0:
                parts += [
                    "",
                    f"<sub>and {dropped} further instance(s) not listed — the "
                    "schema caps this list, so re-run the search in the "
                    "rationale for the full set.</sub>",
                ]

    rule = finding.get("rule_source")
    if isinstance(rule, str) and rule.strip():
        parts += ["", f"<sub>rule: `{rule.strip()}`</sub>"]

    return "\n".join(parts)


def _comment_for(finding: dict) -> dict:
    """Build the inline comment object for a finding.

    Args:
        finding: A single validated finding object.

    Returns:
        A comment dict shaped for the pulls.createReview ``comments`` array.
    """

    end_line = int(finding["end_line"])
    comment = {
        "path": finding["path"],
        "line": end_line,
        "side": "RIGHT",
        "body": _finding_body(finding),
    }

    start_line = finding.get("start_line")
    if isinstance(start_line, int) and start_line < end_line:
        comment["start_line"] = start_line
        comment["start_side"] = "RIGHT"

    return comment


def review_round(prior_reviews: list) -> int:
    """Return which round this review is, counting from 1.

    A round is a **commit this reviewer has already reviewed**, so the answer is
    one more than the number of distinct commits carrying a marked review.

    Three things it is deliberately not:

    - **Not the bot login.** `github-actions[bot]` and a GitHub App installation
      differ, and a switch would silently reset the count and re-open the loop.
    - **Not the marker alone.** The marker is public text: a collaborator could
      paste it into six reviews and switch the floor on permanently, which is the
      paste attack in miniature.
    - **Not the number of review objects.** The workflow fires on `reopened` and
      `ready_for_review`, so a pull request author can close/reopen or toggle
      draft repeatedly, each producing a genuine bot review. Counting distinct
      reviewed commits makes a re-run, a reopen and a draft toggle free.
      *(Verified against #208/#213/#219: every marked review carries a distinct
      `commit_id`, so this reproduces their real counts of 18, 13 and 5.)*

    Args:
        prior_reviews: Review objects from ``pulls/{n}/reviews``.

    Returns:
        The round number, ``1`` when nothing has been reviewed yet.
    """

    seen = set()
    for review in prior_reviews or []:
        if not isinstance(review, dict):
            continue
        user = review.get("user") or {}
        if not isinstance(user, dict) or user.get("type") != "Bot":
            continue
        if MARKER not in str(review.get("body") or ""):
            continue
        seen.add(str(review.get("commit_id") or ""))
    return len(seen) + 1


def build_payload(
    data: dict, provider: str = "", review_round: int = 1, caps: dict | None = None
) -> tuple[dict, str]:
    """Build the review payload and summary body from findings.

    Args:
        data: Decoded findings document matching ``review_findings.schema.json``.
        provider: Name of the provider tier that produced the review. Recorded
            in the footer because the tiers differ materially in capability --
            a reader judging the depth of a review needs to know which model
            wrote it.
        review_round: Which round this is, from :func:`review_round`. At or past
            ``FLOOR_FROM_ROUND`` the suggestion-level findings are withheld and
            the count is stated in the summary. Defaults to 1, so a caller that
            cannot determine the round reports everything -- more review, never
            less.
        caps: Caps from :func:`findings_schema.caps`. ``None`` means enforce
            nothing -- the same direction of failure as ``review_round``: a
            schema this module could not read must never be the reason a
            finding went unreported.

    Returns:
        A ``(payload, summary_body)`` pair. ``payload`` carries the review event
        and inline comments; ``summary_body`` is the standalone comment used
        when the review cannot be attached to the diff.
    """

    data, capped = _apply_caps(data, caps or {})

    findings = [f for f in data.get("findings", []) if isinstance(f, dict)]

    # The floor. Applied before sorting and counting so everything downstream --
    # the tally, the inline comments, `has_blocking` -- sees one consistent set.
    withheld = 0
    if review_round >= FLOOR_FROM_ROUND:
        kept = [
            f for f in findings if str(f.get("severity", "")).lower() in ABOVE_FLOOR
        ]
        withheld = len(findings) - len(kept)
        findings = kept

    findings.sort(
        key=lambda f: (
            SEVERITY_ORDER.get(str(f.get("severity", "")).lower(), 3),
            CONFIDENCE_ORDER.get(str(f.get("confidence") or "").lower(), 1),
        )
    )

    counts = {level: 0 for level in SEVERITY_ORDER}
    for finding in findings:
        level = str(finding.get("severity", "")).lower()
        if level in counts:
            counts[level] += 1

    lines = [MARKER, "## Claude Code review", ""]
    lines.append(str(data.get("summary", "")).strip() or "_No summary returned._")

    # Shown, not merely required. A field the model fills in and nobody reads
    # is a field that stops being filled in honestly -- and this one is the
    # only evidence that the reviewer used the discussion it was given.
    notes = str(data.get("conversation_notes", "")).strip()
    if notes:
        lines += ["", "**From the conversation:** " + notes]

    if findings:
        tally = ", ".join(
            f"{counts[level]} {SEVERITY_LABEL[level].lower()}"
            for level in ("critical", "warning", "suggestion")
            if counts[level]
        )
        lines += [
            "",
            f"**{len(findings)} finding(s):** {tally}.",
            "",
            "<details><summary>All findings</summary>",
            "",
        ]

        # Grouped under severity headings rather than listed flat. The list was
        # already sorted, but a reader scanning it had to infer the boundaries
        # from the bold label on each row; a heading makes "read the criticals,
        # skip the suggestions" a decision they can make without reading.
        for level in ("critical", "warning", "suggestion"):
            in_level = [
                f for f in findings if str(f.get("severity", "")).lower() == level
            ]
            if not in_level:
                continue
            lines += [f"### {SEVERITY_LABEL[level]}", ""]
            for finding in in_level:
                location = f"{finding.get('path')}:{finding.get('end_line')}"
                confidence = str(finding.get("confidence", "")).strip().lower()
                # Only low confidence is worth the reader's attention up front;
                # marking every row would add noise to say nothing.
                mark = " _(low confidence)_" if confidence == "low" else ""
                lines.append(
                    f"- `{location}` — {finding.get('title', '').strip()}{mark}"
                )
            lines.append("")

        # Anything carrying an unrecognised severity would vanish from the
        # groups above, so it gets its own bucket rather than being dropped.
        other = [
            f
            for f in findings
            if str(f.get("severity", "")).lower() not in SEVERITY_ORDER
        ]
        if other:
            lines += ["### Unclassified", ""]
            for finding in other:
                location = f"{finding.get('path')}:{finding.get('end_line')}"
                lines.append(f"- `{location}` — {finding.get('title', '').strip()}")
            lines.append("")

        lines += ["</details>"]
    else:
        lines += ["", "No findings."]

    # The floor must not be invisible. Without this a reader of round 8's review
    # cannot tell a clean round from one the floor emptied -- and a gate whose
    # effect nobody can see is a gate that gets distrusted and switched off.
    if withheld:
        lines += [
            "",
            f"<sub>Review round {review_round}. From round {FLOOR_FROM_ROUND} "
            f"suggestion-level findings are out of scope: **{withheld}** were "
            "withheld this round. Warnings and criticals are reported at every "
            "round, including one introduced by the latest commit.</sub>",
        ]

    # Same principle as the floor disclosure above: a cap whose effect nobody
    # can see is a cap that quietly rewrites reviews. `overflow` is reported
    # rather than applied -- every finding above it is still in the list.
    if any(capped.values()):
        cap_notes = []
        if capped["strings"]:
            cap_notes.append(f"{capped['strings']} over-long field(s) truncated")
        if capped["suggestions"]:
            cap_notes.append(
                f"{capped['suggestions']} over-long code suggestion(s) withheld"
            )
        if capped["instances"]:
            cap_notes.append(f"{capped['instances']} instance list(s) shortened")
        if capped["overflow"]:
            cap_notes.append(
                f"{capped['overflow']} finding(s) over the schema cap, all reported"
            )
        lines += ["", f"<sub>Schema caps applied: {'; '.join(cap_notes)}.</sub>"]

    footer = (
        "<sub>This review never requests changes on the pull request, but the "
        "`review` check fails when no review is produced, so a merge does depend "
        "on it running. It does not run tests, linters, or builds."
    )
    if provider.strip():
        footer += f" Produced by {provider.strip()}."
    lines += ["", footer + "</sub>"]

    summary_body = "\n".join(lines)

    payload = {
        "event": "COMMENT",
        "body": summary_body,
        "comments": [
            _comment_for(f) for f in findings if f.get("path") and f.get("end_line")
        ],
        "marker": MARKER,
        # Derived from the findings rather than trusting the model's own flag,
        # which can contradict the severities it just assigned.
        "has_blocking": counts["critical"] > 0,
        "counts": counts,
    }

    return payload, summary_body


def main() -> int:
    """Read findings and write the review payload.

    Returns:
        0 on success, 1 when the findings file cannot be read or decoded.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--payload-out", required=True)
    parser.add_argument("--summary-out", required=True)
    parser.add_argument(
        "--provider",
        default="",
        help="Provider tier that produced the review, recorded in the footer.",
    )
    parser.add_argument(
        "--schema",
        default=str(findings_schema.DEFAULT_SCHEMA_PATH),
        help=(
            "Findings schema. The caps declared here are enforced on the way "
            "out, because they are stripped from what reaches --json-schema."
        ),
    )
    parser.add_argument(
        "--prior-reviews",
        default="",
        help=(
            "JSON array from pulls/{n}/reviews, used to derive the review round. "
            "Absent or unreadable means round 1 -- err toward reporting MORE: a "
            "flaky call must never be the reason a finding went unreported."
        ),
    )
    args = parser.parse_args()

    try:
        data = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read findings from {args.input_json}: {exc}")
        return 1

    prior = []
    if args.prior_reviews:
        try:
            loaded = json.loads(Path(args.prior_reviews).read_text(encoding="utf-8"))
            prior = loaded if isinstance(loaded, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            # Round 1, deliberately. See the flag's help: the floor must never
            # engage because a fetch failed.
            print(f"warning: cannot read prior reviews: {exc}; treating as round 1")

    # An unreadable schema enforces nothing. Same direction as the prior-reviews
    # fallback above: degradation must report MORE, never less, so a file that
    # cannot be parsed costs a truncation rather than a review.
    caps = None
    try:
        caps = findings_schema.caps(findings_schema.load(args.schema))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: cannot read {args.schema}: {exc}; enforcing no caps")

    current = review_round(prior)
    print(f"review round: {current} (floor from round {FLOOR_FROM_ROUND})")
    payload, summary = build_payload(
        data, provider=args.provider, review_round=current, caps=caps
    )

    Path(args.payload_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.payload_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    Path(args.summary_out).write_text(summary, encoding="utf-8")

    print(f"findings: {len(payload['comments'])} inline, counts={payload['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
