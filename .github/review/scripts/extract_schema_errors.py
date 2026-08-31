"""Recover the schema-validation evidence from a review run's execution record.

When the reviewer cannot express its review in the shape ``--json-schema``
demands, the CLI's ``StructuredOutput`` tool rejects the attempt with a message
that **already names the offending field and the constraint** — for example
``/findings/3/title: must NOT have more than 120 characters``. That message is
the single most useful thing about the failure, and until upstream it was thrown
away: the job kept only the last sixty lines of the record, and the validator
messages are not in the tail.

🔴 **This is deliberately a separate module writing a separate file, and that
separation is load-bearing.** The messages live in **tool results**, which
``interpret_claude_result.py`` excludes from its haystack on purpose: upstream
measured a genuine transient ``server_error`` reported as ``fatal`` — with
instructions to top up a balance that was not spent — because the classifier was
matching provider-error vocabulary against text the reviewer had merely *read*.
Nothing here is imported by the classifier, and nothing it writes is fed back
into one. This file is for a human reading the artifacts.

⚠️ **The terminal reason cannot be relied on to say a schema failure happened.**
Measured on the runner (upstream probe, run 32491761024): a run whose every
attempt was rejected by the validator ended ``subtype=success``,
``terminal_reason=completed``, with no structured output — it never reached
``error_max_structured_output_retries``. The classifier resolves that shape
through its generic fallthrough, *"ran but returned no payload and no
recognisable error"*. This file is what turns that into a diagnosis.
"""

from __future__ import annotations

import argparse
import os
import json
import re
import shutil
from pathlib import Path

#: The literal the CLI's StructuredOutput tool raises. Its own source, read off
#: the binary the workflow installs:
#:
#:   throw new ht(`Output does not match required schema: ${l}`,
#:                `StructuredOutput schema mismatch: ${c ?? ""}`)
#:
#: where ``l`` joins ``${instancePath || "root"}: ${message}`` over ajv's errors
#: and ``c`` joins their ``keyword`` names.
REJECTION = "Output does not match required schema"


#: Which JSON Schema keyword each ajv message means, so the report can name the
#: keyword an operator has to remove rather than only quoting the prose.
#:
#: ⚠️ **These strings are this build's, not ajv's in general.** Read off the
#: runner (upstream probe): the maxLength message here is
#: ``must NOT have more than N characters``, while a different Claude Code build
#: on a developer machine says ``must NOT be longer than``. Both spellings are
#: matched, and an unrecognised message still has its full text printed below --
#: the tally is a convenience, never the evidence.
#:
#: ⚠️ The trailing noun is the discriminator, not the comparison. ``must NOT
#: have more than N characters`` is ``maxLength`` and ``must NOT have more than
#: N items`` is ``maxItems`` -- matching on "more than" alone would report both
#: every time either one fired.
_KEYWORD_SIGNATURES = (
    ("maxLength", r"(?:more|longer) than \d+ characters"),
    ("minLength", r"(?:fewer|shorter) than \d+ characters"),
    ("maxItems", r"(?:more|longer) than \d+ items"),
    ("minItems", r"(?:fewer|shorter) than \d+ items"),
    ("maximum", r"must be <="),
    ("minimum", r"must be >="),
    ("pattern", r"must match pattern"),
)

# ⚠️ The CLI's second message -- `StructuredOutput schema mismatch: <keywords>` --
# is NOT extracted, because it never reaches the record. It is the exception's
# short-message argument and stays internal: measured zero occurrences in a
# transcript carrying two real rejections (upstream probe, run 32491761024). A
# first version of this file looked for it and would have printed an empty
# "failing keywords" section on every genuine failure. The keyword names are
# recoverable from the messages themselves, which is what the report does.


def _tidy(message: str) -> str:
    """Reduce a rejection to the part that names fields and constraints.

    The record is decoded as JSON before it reaches here, so there is no
    escaping left to undo — an earlier version of this module unescaped by hand
    and got ``\\n`` and ``\\\\`` in the wrong order. Only the framing is cut.

    Args:
        message: A rejection message as the CLI wrote it.

    Returns:
        The message without its ``Error:`` prefix or the marker itself.
    """

    tidied = message.strip()
    if tidied.startswith("Error:"):
        tidied = tidied[len("Error:") :]
    head, marker, tail = tidied.partition(REJECTION)
    if marker:
        tidied = tail if not head.strip() else tidied
    return tidied.strip().lstrip(":").strip()


def _events(text: str) -> list | None:
    """Decode the record into events, accepting both shapes it comes in.

    Args:
        text: Raw execution record.

    Returns:
        The decoded events, or ``None`` when the text is not JSON at all — a
        bare CLI error message, which has no events and no tool results in it.
    """

    stripped = text.strip()
    if not stripped:
        return None
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        return decoded if isinstance(decoded, list) else [decoded]

    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events or None


#: The terminal event's own outcome fields, scoped exactly as
#: ``interpret_claude_result.OUTCOME_FIELDS``: the CLI's words about the RUN,
#: never a file the reviewer read. ``message`` and ``content`` are deliberately
#: absent for the same reason there.
#:
#: ``error_max_structured_output_retries`` -- the failure this ticket is named
#: for -- lands here rather than in an error-flagged tool result.
TERMINAL_FIELDS = ("error", "result", "subtype", "terminal_reason")


def _strings_in(value: object) -> list[str]:
    """Collect the string leaves of one outcome field.

    Anthropic-shaped errors arrive as an object, so a string-only reading would
    skip the very field it exists to look at.

    🔴 **Deliberately UNBOUNDED in depth, unlike the near-identical helper in
    ``interpret_claude_result.py``, and the difference is the reason rather than
    an oversight.** There the cap is a **safety** bound: it limits what may vote
    on a verdict, so a miss is the safe direction. Here it is a **reach** bound:
    it limits what can be found, so a miss is the *unsafe* direction — and a
    missed rejection is the failure this module has now been fixed for twice.
    A copied ``_MAX_DEPTH = 3`` silently lost anything nested deeper.

    It protects nothing concrete either: ``json.loads`` already refuses nesting
    past the interpreter's recursion limit, so anything that parsed can be
    walked, and only strings containing the marker are kept downstream.

    ⚠️ **Do not "re-sync" this with `interpret_claude_result._strings_in`.** The
    two have diverged twice on purpose — that one also caps each string at
    ``OUTCOME_FIELD_CHARS``, this one must not, because here the whole message
    is the evidence.

    Args:
        value: An outcome field's value.

    Returns:
        Every string leaf.
    """

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings_in(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings_in(item)]
    return []


def _rejections_in(node: object, flagged: bool = False) -> list[str]:
    """Collect rejection messages carried by **error-flagged** results only.

    Args:
        node: Any part of a decoded event.
        flagged: True once inside a node whose ``is_error`` is set.

    Returns:
        Every rejection message reachable under an error flag.
    """

    if isinstance(node, dict):
        here = flagged or bool(node.get("is_error"))
        out = []
        for value in node.values():
            if here and isinstance(value, str) and REJECTION in value:
                out.append(value)
            else:
                out.extend(_rejections_in(value, here))
        return out
    if isinstance(node, list):
        return [msg for item in node for msg in _rejections_in(item, flagged)]
    return []


def attempts(text: str) -> list[str]:
    """Return the rejection messages from each rejected attempt.

    🔴 **Scoped to error-flagged tool results, and that is the whole point.**
    A first version searched the record as raw text for the rejection literal.
    The record carries **every tool result**, so any file the reviewer merely
    *read* that contains that literal counted as a rejected attempt — and this
    module's own source contains it. Measured on a record with **zero** real
    rejections whose reviewer had read this directory: the report claimed *"2
    attempts rejected"* and named five constraints, none of which had fired.
    That is upstream's failure reproduced one layer out, in the file that
    documents upstream. The discriminator is ``is_error`` on the ``tool_result``
    block, verified against a real transcript.

    ⚠️ **Distinct messages within one event are all kept.** The CLI writes each
    rejection twice into the same event — once under
    ``message.content[].content`` and once under ``tool_use_result`` — so the
    duplicates are dropped; but a version that kept only the *first* message per
    event would silently discard a second, genuinely different one.

    🔴 **The terminal event's own outcome fields are read too, and leaving them
    out made this blind to the ticket's headline failure.** ``is_error`` alone
    was verified against the probe transcript — and ``PROBE.md`` records that
    the probe **never reached** ``error_max_structured_output_retries``: its
    subject ended ``subtype=success``, ``is_error=False``. So the one shape this
    file exists for is the shape the transcript could not confirm, and a
    terminal event carrying the message in ``result`` with no error flag read as
    zero. :data:`TERMINAL_FIELDS` is scoped exactly as
    ``interpret_claude_result.OUTCOME_FIELDS`` — the CLI's words about the run,
    never a file the reviewer read — so it widens the reach without reopening
    the false-positive above.

    ⚠️ **Still narrower than the record.** A rejection that reaches neither an
    error-flagged result nor a terminal outcome field is invisible here. The
    report says so rather than asserting there was none.

    Args:
        text: Raw execution record.

    Returns:
        The rejection messages, in order, duplicates within an event removed.
    """

    return _collect(text)[0]


def _collect(text: str) -> tuple[list[str], int]:
    """Return the rejection messages and how many raw markers they consumed.

    🔴 **The consumed count is what makes the report's stray note honest.** A
    raw occurrence count minus a **de-duplicated** message count is not a miss —
    it is the CLI's double-write — and comparing the two made the note fire on
    **every** genuine failure: two real rejections read as *"the marker appears
    2 further time(s)… ALSO POSSIBLE: a genuine rejection this scoping does not
    reach"*. A warning that fires on 100% of hits is one a reader learns to
    skip, which disarms it exactly when it matters.

    Args:
        text: Raw execution record.

    Returns:
        A ``(messages, consumed)`` pair.
    """

    events = _events(text)
    if events is None:
        return [], 0

    found: list[str] = []
    consumed = 0
    for event in events:
        per_event = _rejections_in(event)
        # A terminal event can be BOTH error-flagged and carry the message in an
        # outcome field, so the two readings overlap and are deduplicated
        # together rather than separately -- counting each path on its own
        # reported that one rejection twice.
        if isinstance(event, dict) and event.get("type") == "result":
            per_event += [
                value
                for field in TERMINAL_FIELDS
                for value in _strings_in(event.get(field))
                if REJECTION in value
            ]
        # An event that yielded a rejection accounts for EVERY marker in that
        # event, not just the copies this reader collected. The CLI's second
        # copy lives under `tool_use_result`, a sibling of `message` carrying no
        # error flag, so the scoped reader never touches it -- and counting only
        # what was collected left those copies looking like a possible miss on
        # every genuine failure. Markers in events that yielded NOTHING are the
        # genuinely unread ones, and those are what the note exists to surface.
        #
        # `json.dumps` is safe as a counter here: the marker contains no
        # character JSON escapes, so it survives serialisation intact.
        if per_event:
            consumed += json.dumps(event).count(REJECTION)
        # De-duplicated on the TIDIED message, not the raw one: the same
        # rejection arrives with different framing (`Error: ` prefixed under
        # `tool_use_result`, bare under `content`), so de-duplicating on the raw
        # string lets one rejection through twice wherever both readings land on
        # one event -- which is exactly what the terminal branch above does.
        found.extend(dict.fromkeys(_tidy(value) for value in per_event))
    return found, consumed


def unscoped_marker_count(text: str) -> int:
    """Count raw occurrences of the rejection literal, ignoring the error flag.

    Used to explain what the scoped reading did **not** consume, on every
    report rather than only an empty one — a *partial* miss is the case an
    empty-only footnote hides. It is the number :func:`attempts` deliberately
    does not report, so a reader who suspects a real failure is not left with a
    bare "none found" that could equally mean "this file cannot see it any
    more".

    Args:
        text: Raw execution record.

    Returns:
        How many times the literal appears anywhere in the record.
    """

    return text.count(REJECTION)


def report(text: str) -> str:
    """Render the schema-validation evidence a failed run left behind.

    Args:
        text: Raw execution record.

    Returns:
        A human-readable report. Always non-empty: a report that said nothing
        when it found nothing would be indistinguishable from one that failed to
        run, and this file exists precisely for the runs nobody can otherwise
        explain.
    """

    # Already tidied and de-duplicated by `_collect`, which must de-duplicate on
    # the tidied form to see through the two framings the CLI uses.
    rejections, consumed = _collect(text)

    lines = [
        "Schema validation evidence (upstream)",
        "=" * 36,
        "",
        "What the CLI's StructuredOutput tool said when it refused an attempt.",
        "Each line names the failing field path and the constraint it broke.",
        "",
        "This file is NOT read by interpret_claude_result.py -- it comes from",
        "tool results, which the classifier excludes on purpose. Read it, do not",
        "wire it into anything.",
        "",
    ]

    # 🔴 Printed on EVERY report, not only the empty one. The scoped reading is
    # narrower than the record, so the raw count is the reader's only signal
    # that this file may be looking in the wrong place -- and a PARTIAL miss
    # (three found, two not) is exactly the case an empty-only footnote hides.
    #
    # ⚠️ Stated as a hypothesis, never as a finding. An earlier version asserted
    # that the strays "are text the reviewer READ, not attempts it made". This
    # file cannot know that: the same shape occurs when a genuine rejection
    # arrives somewhere the scoping does not reach. Asserting it steered a
    # reader away from the correct explanation, in the file whose whole purpose
    # is the runs nobody can otherwise explain.
    # ⚠️ Against what was CONSUMED, never against `len(rejections)`. The CLI
    # writes each rejection twice and `_collect` collapses that, so subtracting
    # the message count would report the collapsed duplicates as a possible
    # miss -- on every genuine failure. See `_collect`.
    stray = unscoped_marker_count(text) - consumed
    footnote = []
    if stray > 0:
        footnote = [
            "",
            f"Note: the marker appears {stray} further time(s) in the record,",
            "outside anything this file reads (an error-flagged tool result, or",
            "the terminal event's own outcome fields).",
            "",
            "  MOST LIKELY: text the reviewer READ -- this module's own source",
            "  contains the marker, so any review of this directory produces some.",
            "  ALSO POSSIBLE: a genuine rejection in a record shape the scoping",
            "  does not reach. If a failure is unexplained, grep the raw record",
            "  in claude_execution_record.json before concluding there was none.",
        ]

    if not rejections:
        lines += [
            "No StructuredOutput rejection found where this file looks.",
            "",
            "That is consistent with a review that did not fail schema",
            "validation -- but it is not proof of one: the reading is scoped",
            "(see attempts()). If the run produced no review, check",
            "claude_diagnostic.txt, and the note below if there is one.",
        ]
        return "\n".join(lines + footnote) + "\n"

    lines.append(f"{len(rejections)} attempt(s) rejected.")
    lines += footnote

    # The keywords, recovered from the messages rather than from the CLI's
    # second message, which never reaches the record. This is the line that
    # answers "which constraint cost us the review".
    seen = [
        keyword
        for keyword, needle in _KEYWORD_SIGNATURES
        if any(re.search(needle, message) for message in rejections)
    ]
    if seen:
        lines += ["", f"Constraints broken: {', '.join(seen)}."]

    lines += ["", "Distinct messages:"]
    for message in dict.fromkeys(rejections):
        lines += ["", f"  {message}"]

    lines += [
        "",
        "",
        "Fixing it: a cap the model broke should not be reaching the CLI at all.",
        "findings_schema.py strips the constraint keywords and post_review.py",
        "enforces them afterwards. A keyword named above that is still being sent",
        "means CONSTRAINT_KEYWORDS is missing it.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Copy the execution record into the artifacts and write the evidence.

    Returns:
        Always 0. Missing evidence must never be the reason a run fails — the
        run has already failed for its own reasons by the time this runs, and a
        non-zero exit here would replace a real diagnosis with this file's own.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Evidence report destination.")
    parser.add_argument(
        "--record-out",
        default="",
        help="Where to copy the full execution record. Empty to skip the copy.",
    )
    args = parser.parse_args()

    path = os.environ.get("CLAUDE_EXECUTION_FILE") or ""
    if not path:
        fallback = (
            Path(os.environ.get("RUNNER_TEMP", "")) / "claude-execution-output.json"
        )
        if fallback.is_file():
            path = str(fallback)

    # 🔴 Every filesystem call below is inside this guard, and that is the whole
    # point of the function. This step sits between `Interpret result` and
    # `Resolve outcome`; a non-zero exit here fails the job, and every later step
    # is gated on an implicit `success()` -- so an unreadable record or a full
    # disk would skip `Post review` and suppress a review that had already
    # succeeded. Losing the evidence for a failure is a bad day; losing a
    # good review to the machinery that documents failures is worse.
    #
    # The step also carries `continue-on-error`, so this is the inner half of a
    # belt and braces. Both are deliberate: the flag protects against a crash
    # this function did not anticipate, and this guard reports WHAT went wrong
    # into the artifact rather than only into a step annotation nobody opens.
    try:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)

        record = Path(path) if path else None
        if record is None or not record.is_file():
            destination.write_text(
                "Schema validation evidence (upstream)\n"
                "====================================\n\n"
                "No execution record was produced, so there is nothing to read.\n"
                "Claude never reached the model -- see claude_diagnostic.txt.\n",
                encoding="utf-8",
            )
            print("no execution record; wrote a placeholder")
            return 0

        text = record.read_text(encoding="utf-8", errors="replace")
        # Walked once. `report` and the closing line both need the count, and
        # the walk is a full JSON decode plus a recursive descent over a record
        # that can run to megabytes -- cheap to do twice only when it was a
        # text split, which it no longer is.
        rejected = len(attempts(text))
        destination.write_text(report(text), encoding="utf-8")

        # The full record, not the sixty-line tail the diagnostic carries. Every
        # later step in diagnosing one of these failures is a guess without it.
        if args.record_out:
            target = Path(args.record_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record, target)
            print(f"copied {record} -> {target} ({target.stat().st_size} bytes)")

        print(f"schema evidence: {rejected} rejected attempt(s) -> {destination}")
    except OSError as exc:
        # Reported, never raised. `KeyboardInterrupt` and `SystemExit` are not
        # caught -- they are runner cancellation, not a failure of this step.
        print(f"::warning::could not write the schema evidence: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
