"""Interpret the outcome of one ``claude-code-action`` attempt.

``anthropics/claude-code-action`` reports a generic "did not return
structured_output" error for any empty result. That masks situations that call
for opposite responses, so this module classifies each attempt into one of
three outcomes:

* **ok** — a valid findings payload came back, **and it is actually a review**
  (see :func:`is_substantive`).
* **exhausted** — the provider could not serve the request: quota spent, key
  rejected, model unavailable, a transient error, **an answer that satisfied
  the schema while saying nothing**, or **a call that burned its whole timeout
  budget**. Re-running may work. ⚠️ Since upstream this also covers an answer
  that said plenty and left ``conversation_notes`` blank — the one case here
  where the provider answered *fully*, so "could not serve the request"
  describes the route rather than the cause.
* **fatal** — the failure is in the workflow itself: the CLI rejected the
  arguments, or Claude never reached the model. Re-running is unlikely to help.

⚠️ **Since upstream duration is an input to the classification, not only to the
retry.** A model call killed by ``API_TIMEOUT_MS`` loses its execution record —
the action writes that file only after a call returns — so it used to arrive
looking exactly like a run that never launched, and both were called ``fatal``.
:func:`timed_out` separates them, and it is derived **once**, in :func:`classify`,
and read by everything downstream.

⚠️ **The two verdicts are not equally definite, and the wording reflects it.**
"Quicker than one call's budget" genuinely rules a timeout out; "as long as the
budget" only makes one possible, and what carries it is that a bad endpoint or an
unroutable model fails in *seconds*. So ``fatal`` says the settings are worth
checking rather than that they are wrong.

⚠️ **Since upstream the status does not decide whether the workflow retries.**
:func:`retry_verdict` does, and it now splits **both** statuses: a ``fatal`` that
named nothing an operator could fix and finished quicker than a single model call
is retried because it billed nothing, and the one ``exhausted`` that burned a
whole budget is *not*, because it did. Read the emitted ``retryable`` key, never
the status name — reading the status name is the mistake upstream was filed over,
and upstream made the two diverge further.

**Both fail the job.** There is one provider and no fallback chain, so a
failure of either kind means the pull request was not reviewed, and a green
check would claim otherwise.

The split is kept anyway because it answers *who fixes this*: top up a balance,
or edit a workflow. Collapsing the two would leave every failure reading like a
misconfiguration, and the most common one — a spent balance — is not.

Adopted from an internal repository, where the
classification originally also chose control flow across a three-provider
fallback chain. This repository has never had that chain; the split survives
here purely for the diagnostic distinction above.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

# Provider-specific: this key or endpoint cannot serve the request. These stay
# separate from the fatal set because the fix is different -- top up, swap a
# key, or simply wait. Transient errors live here because re-running can clear
# them, which a workflow bug never does.
EXHAUSTED_PATTERNS = (
    r"\brate[_ ]?limit",
    r"\b429\b",
    r"\boverloaded\b",
    r"\bserver_error\b",
    r"\b50[023]\b",
    r"\bcapacity\b",
    r"\btimeout\b",
    r"\btemporarily unavailable\b",
    r"\bconnection (reset|refused|error)\b",
)

# The model ran the review and then failed to express it in the schema the CLI
# was given. This is NOT a provider outage and NOT a spent balance: the turns
# happened and were billed. Kept as its own set so the diagnostic can say that,
# because the generic fallthrough calls it "no recognisable error" -- and the CLI
# writes this subtype into the execution record by name.
STRUCTURED_OUTPUT_PATTERNS = (
    r"error_max_structured_output_retries",
    r"did not return structured_output",
)

# 🔴 Kitty's own launch failures are invisible to everything below this line.
# It prints them to the wrapper's stderr and NOWHERE else, and
# `claude-code-action` writes an execution record carrying none of that stream.
# So a bridge that never launched reaches `classify` as an EMPTY record and
# comes out as "no execution record; Claude never reached the model" -- true,
# useless, and pointing at a list of settings that are all correct.
#
# The wrapper tees that stream to a file and the workflow passes its path in
# `KITTY_STDERR_FILE`. These sets read it.
#
# ⚠️ EGRESS IS CHECKED BEFORE THE PROXY-AUTH SET, and the order is load-bearing.
# The sibling repository measured both against the same gateway: a wrong
# password answers **407**, while a correct password from an address the
# gateway does not allow answers **403**. The 403 message also carries the word
# "proxy", so a proxy-auth rule matching first would send an operator to
# re-copy a credential that is already right.
BRIDGE_EGRESS_PATTERNS = (
    r"through the egress proxy",
    r"egress[^\n]{0,80}\b403\b",
    r"\b403\b[^\n]{0,80}egress",
)

BRIDGE_PROXY_AUTH_PATTERNS = (
    r"\b407\b",
    r"proxy authentication required",
)

# A profile whose credential will not resolve, or a store kitty cannot read.
# `NonTTYError` is the specific shape of "kitty fell through to its interactive
# setup wizard", which off a terminal is what a version-mismatched or empty
# profile store does.
BRIDGE_PROFILE_PATTERNS = (
    r"nonttyerror",
    r"no profile",
    r"unknown profile",
    r"could not resolve[^\n]{0,40}credential",
)

# Checked BEFORE the generic patterns: an exhausted plan reports itself as a
# 429 with error "rate_limit", which EXHAUSTED_PATTERNS would match first and
# describe as a transient blip. Both classify as `exhausted`, but the diagnostic
# wording differs and quota is the case worth naming precisely -- it is the one
# an operator fixes with a credit card rather than a re-run.
#
# Several entries below cannot fire against the endpoint this repository uses.
# They are inherited from the source repository, where each was added after a
# real observed failure, and they are kept for two reasons: they cost one regex
# match each, and a set that only recognises the current provider's vocabulary
# is exactly how the source repository once misread a spent plan as a transient
# blip. Anything reached through an Anthropic-compatible endpoint may phrase a
# quota failure in any of these ways.
QUOTA_PATTERNS = (
    # Coding-plan style limits, observed upstream. Both were seen as a 429 whose
    # body carried a numeric code and a reset timestamp, for example:
    #   API Error: Request rejected (429) - [1308][Usage limit reached for 5
    #   hour. Your limit will reset at 2026-07-26 23:56:57]
    #   1308  Usage limit reached for 5 hour
    #   1310  Weekly/Monthly Limit Exhausted
    r"usage limit reached",
    r"weekly/monthly limit exhausted",
    r"limit will reset",
    r"\b1308\b",
    r"\b1310\b",
    r"\b1113\b",
    r"insufficient balance",
    # upstream: OpenRouter's wording for the same condition. It bills from a
    # prepaid credit balance and documents a spent one as "insufficient
    # credits", which matched NOTHING in this set -- every entry above was
    # written against a provider that says "balance".
    #
    # 🔴 The status code is deliberately NOT matched. A first version of this
    # added `\b402\b` beside it and reproduced, in the module that documents the
    # failure twice, the exact bug it documents: `_outcome_text` includes
    # `result`, which on a schema failure is the model's OWN prose, so
    # "interpret_claude_result.py:402" or "billed $0.402" classified as
    # `exhausted` and told an operator to top up a balance that was not spent.
    # The wording alone catches the real 402 -- its body carries
    # "Insufficient credits" -- so the code bought nothing and cost that.
    r"insufficient credits",
    # Generic quota and billing.
    r"quota",
    r"exceeded your current",
    r"\bbilling\b",
)

# Credentials and model resolution. Provider-specific by definition: a different
# key, or a different provider's model name, is exactly what may fix them.
CREDENTIAL_PATTERNS = (
    r"\bauthentication_failed\b",
    r"\b401\b",
    r"\b403\b",
    r"model_not_found",
    r"\bmodel not found\b",
)

# Universal: the workflow itself is wrong and any provider would reject it the
# same way, so re-running is pure waste. Both failures seen on the first live
# run land here -- the apostrophes that truncated --json-schema, and the
# unresolvable $schema reference.
FATAL_PATTERNS = (
    r"is not valid json",
    r"is not a valid json schema",
    r"invalid[_ ]request",
    r"\b400\b",
    r"unterminated string",
)

# upstream. Anchored on the beta's own dated slug, or on the refusal's distinctive
# phrasing, and NOT on the bare words "context management".
#
# 🔴 The loose form was written first and is the bug this module documents twice:
# `_outcome_text` includes `result`, which on a schema failure carries the
# model's own prose, so a review that merely discussed context management would
# have been told its own failure was unfixable. A classifier that pattern-matches
# a haystack it does not control will eventually match itself.
CONTEXT_MANAGEMENT_REFUSAL = (
    r"context-management-\d{4}-\d{2}-\d{2}"
    r"|no endpoints available[^\n]{0,80}context[-. ]management"
)

#: What the CLOCK says, one per reachable state — and NOTHING else, because this
#: is the only sentence in the record-absent body that is true on every path
#: into it.
#:
#: 🔴 **One opening for all three states was the first version, and design review
#: caught it doing exactly what this ticket was filed about.** It began *"the
#: attempt finished inside the budget … so it was not a timeout"* and appended a
#: retraction for the unmeasured case at the very bottom — so a reader with no
#: measurement at all was told a duration finding in the first sentence and
#: corrected fifteen lines later, after the settings list had already sent them
#: to a settings page. The retraction's own comment said not to do that.
UNMEASURED_OPENING = (
    "Claude produced no execution record, and the attempt was NOT TIMED -- the "
    "workflow's stamp step did not run or wrote something that is not a "
    "number, or API_TIMEOUT_MS could not be read as one. So the duration test "
    "that separates a hung provider from a setup fault could not be applied "
    "here, and what follows is a default rather than a finding: read the "
    "run's own duration on the run page first. A healthy review here takes "
    "7-12 minutes."
)

REACHED_THE_MODEL_OPENING = (
    "Claude produced no execution record, and the attempt ran long enough to "
    "have reached the model. The action writes the record only after a call "
    "returns, so a call killed by the API_TIMEOUT_MS budget loses it having "
    "burned the whole thing -- which is why an empty record alone says very "
    "little."
)

INSIDE_THE_BUDGET_OPENING = (
    "Claude produced no execution record, and the attempt finished inside the "
    "budget for a single model call -- so it was not a timeout. (The action "
    "writes the record only after a call returns, so a call killed by that "
    "budget loses it having burned the whole thing, which is why the run "
    "measures the attempt rather than guessing.)"
)

#: The settings an operator can actually change. Printed under whichever opening
#: above applies, and never under a timeout — that list is the cost this ticket
#: is filed about.
NO_RUN_ADVICE = (
    "⚠️ What the line above rules out is a timeout; it does not by itself prove "
    "the workflow is misconfigured. The setup faults measured under this reason "
    "came in at 40s and 43s, and a healthy review takes 7-12 minutes, so an "
    "attempt in the minutes below the budget is in a band nothing has measured "
    "-- read the log above before trusting this list over it. Check, in this "
    "order:\n"
    "  1. The arguments in claude_args -- a malformed --json-schema is the "
    "failure that has actually occurred here, twice. The schema-loading step "
    "guards against apostrophes and a remote $schema reference; anything else "
    "malformed shows up as a CLI rejection in the log above.\n"
    "  2. The KITTY_CREDENTIALS_JSON repository secret -- the action gates "
    "its own startup on the anthropic_api_key input and does not read the "
    "environment; the input is fed the credential store (kitty overrides "
    "the value in the child, so the gate value never reaches the provider), "
    "and an unset secret stops it launching before any of the settings "
    "below matter.\n"
    "  3. The KITTY_* repository settings -- the Configure step reports "
    "available=false naming any setting that is missing or not valid JSON, "
    "and the review step then never launches. When configuration succeeded, "
    "the runner log above is where the bridge launching and resolving the "
    "claude binary appear: a bridge that fails to launch, a missing claude, "
    "or a misconfigured wrapper each stops the run before any model is "
    "reached.\n"
    "  4. The action's own setup steps in the log above.\n"
    "\n"
    "Workflow consistency is NOT a likely cause. The action checks this file "
    "against the base branch, but in practice it has not blocked a pull "
    "request that edits its own workflow."
)

# The other side of the split upstream exists to draw. The action writes its
# execution record only after a call returns, so an attempt killed by
# `API_TIMEOUT_MS` loses the record HAVING BURNED THE WHOLE BUDGET, and arrives
# looking exactly like a run that never launched.
#
# ⚠️ It deliberately does NOT list settings to check. That list is the cost this
# ticket is filed about: it is confident, prescriptive, and sends an engineer to
# edit a workflow that is correct. Measured twice at 20m35s (PR #397, PR #345),
# each refuted by a plain re-run that passed in about seven minutes.
TIMED_OUT_DIAGNOSIS = (
    "The attempt ran for at least the whole API_TIMEOUT_MS budget for a single "
    "model call, so it was long enough to have reached the model -- and a "
    "misconfigured endpoint or an unroutable model fails in seconds. So this is "
    "the provider rather than the workflow, and re-running is the fix: this "
    "exact shape has been cleared by a plain re-run twice.\n"
    "\n"
    "⚠️ It is NOT retried automatically, and that is a cost decision rather "
    "than a doubt about the diagnosis. A timed-out call was billed for the "
    "whole budget it burned, so a second one is spent on the run that "
    "already cost the most. Re-run it by hand when you want it.\n"
    "\n"
    "If this recurs on every run rather than intermittently, the lever is "
    "API_TIMEOUT_MS in claude-code-review.yml or the amount the reviewer is "
    "being asked to read -- never the KITTY_* settings, which would have "
    "failed the run in seconds."
)


# An empty-findings review must still say what was checked -- REVIEW_PROMPT.md
# requires it in those words, "so a reader can tell an empty result from an
# unperformed review". This is the number that makes the requirement real.
#
# Only applied when `findings` is empty, so it can never reject a review that
# found something. 200 characters is roughly two sentences: comfortably below
# any genuine "here is what I checked and why it is clean", and far above the
# degenerate case.
#
# ⚠️ True of THIS floor, not of `is_substantive`, which since upstream also
# enforces `conversation_notes` unconditionally and does reject a review that
# found something. See that function for why the two are gated differently.
MINIMUM_EMPTY_REVIEW_SUMMARY = 200


def _conversation_notes(payload: dict) -> str:
    """Return a payload's conversation notes, stripped, or ``""`` when it has none.

    One definition of "blank", shared by :func:`is_substantive` and by the
    diagnosis :func:`main` writes, so the verdict and the reason given for it
    cannot drift apart.

    🔴 **A non-string is blank, and ``str(value)`` would not say so.**
    ``str(None).strip()`` is ``"None"`` -- non-blank, and rendered to the pull
    request as ``From the conversation: None``. That is reachable:
    :func:`_as_findings_payload` accepts any dict carrying a ``findings`` key,
    and the execution-record recovery path uses it, so a payload arriving that
    way has passed no validator at all -- not even for ``type``.

    Args:
        payload: A decoded findings document.

    Returns:
        The stripped notes, or the empty string when the field is absent, not a
        string, or contains only whitespace.
    """

    value = payload.get("conversation_notes")
    return value.strip() if isinstance(value, str) else ""


def is_substantive(payload: dict) -> bool:
    """Report whether a schema-valid payload is actually a review.

    Observed upstream on 2026-08-01: the provider returned
    ``{"summary": "Test minimal call.", "findings": [],
    "conversation_notes": "Test."}``. Every schema constraint was satisfied --
    both strings are non-empty -- so the workflow classified it ``ok``, posted
    "No findings", and went green. A reader sees a clean review; nothing
    reviewed the change.

    That is the precise failure this workflow exists to prevent, arriving
    through the one door it did not watch: not a provider that refused, but a
    provider that answered emptily.

    ⚠️ **Two rules, gated differently, and the difference is deliberate
    (upstream).** The summary floor is a LENGTH judgement, so it applies only to
    an empty review and can never discard findings. The
    ``conversation_notes`` rule applies to every payload, findings or not,
    because a blank value there is not terse but absent -- and ajv rejected it
    unconditionally until upstream stopped shipping the keyword. This restores
    that rather than inventing a new bar.

    Args:
        payload: A decoded findings document.

    Returns:
        False when the conversation notes are blank. Otherwise True when the
        payload carries findings, or an empty-findings summary long enough to
        be an account of what was checked.
    """

    # Checked first, and independently of findings: this is the only field that
    # records whether the pull request discussion was read at all, and a blank
    # one renders as nothing rather than as a visible gap.
    if not _conversation_notes(payload):
        return False
    if payload.get("findings"):
        return True
    return len(str(payload.get("summary", "")).strip()) >= MINIMUM_EMPTY_REVIEW_SUMMARY


def _read_execution_record(path: str | None) -> str:
    """Read the action's execution log as raw text.

    Args:
        path: Path to the execution output file, if the action provided one.

    Returns:
        File contents, or an empty string when the file is absent or unreadable.
    """

    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_file():
        return ""
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _as_findings_payload(value: object) -> dict | None:
    """Coerce a value into a findings payload if it is one.

    Accepts either a decoded object or a JSON string, since the action's output
    binding and the execution record disagree about which they carry.

    Args:
        value: Candidate value from an output binding or execution event.

    Returns:
        The payload dict, or None when the value is not a findings payload.
    """

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict) and "findings" in value:
        return value
    return None


def _extract_structured_output(raw_output: str, execution_text: str) -> dict | None:
    """Recover the findings payload from whatever the action returned.

    Tries the action's ``structured_output`` first, then scans the execution
    record, which still contains the payload in failure shapes where the output
    binding came back empty.

    Args:
        raw_output: Value of the action's ``structured_output`` output.
        execution_text: Raw execution record text.

    Returns:
        The decoded payload, or None when no valid payload is present.
    """

    payload = _as_findings_payload((raw_output or "").strip())
    if payload is not None:
        return payload

    # The execution record is newline-delimited JSON; the result event carries
    # the payload even when the output binding did not.
    for line in execution_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("structured_output", "result"):
            payload = _as_findings_payload(event.get(key))
            if payload is not None:
                return payload
    return None


#: Event fields that describe the OUTCOME of a run, as opposed to its content.
#:
#: 🔴 **The haystack must not include what the model READ.** The execution record carries every
#: tool result, so a review that opens a file puts that file's text where the provider-error
#: patterns are searched. Reviewing a change under `.github/review/scripts/` therefore fed this
#: module's own source into its own matcher: `interpret_claude_result.py` contains the literals
#: ``\b400\b``, ``quota``, ``insufficient balance`` and ``billing``, and one read of it matches
#: **nine** of :data:`QUOTA_PATTERNS` and three of :data:`FATAL_PATTERNS`.
#:
#: Measured on PR #237: a genuine transient ``server_error`` — whose correct verdict is
#: ``exhausted`` and whose correct advice is "re-run" — was reported as ``fatal``, *"re-running
#: will not fix this"*, with instructions to top up a balance that was not spent. The sibling
#: repository served a review successfully 44 seconds earlier on the same provider.
#:
#: ⚠️ ``message`` and ``content`` are deliberately absent: that is where tool results and model
#: prose live, and both quote the code under review.
OUTCOME_FIELDS = (
    "error",
    "result",
    "subtype",
    "api_error_status",
    "terminal_reason",
    "stop_reason",
)

#: Per-field cap, so one oversized field cannot reintroduce the problem above.
OUTCOME_FIELD_CHARS = 4000


def _parse_events(execution_text: str) -> list | None:
    """Decode the execution record into events, accepting both shapes it comes in.

    The record has been observed as a pretty-printed JSON **array** and is documented
    elsewhere in this file as newline-delimited JSON. Both are handled rather than one being
    declared correct, because a shape this function does not recognise must degrade to
    "unparseable" — never to "no errors found".

    Args:
        execution_text: Raw execution record text.

    Returns:
        The decoded events, or ``None`` when the text is not JSON at all (a bare CLI error
        message, which every caller should then search whole).
    """

    stripped = execution_text.strip()
    if not stripped:
        return None
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        return decoded if isinstance(decoded, list) else [decoded]

    events = []
    for line in execution_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events or None


def _outcome_text(execution_text: str) -> str | None:
    """Return only the outcome-bearing fields of the record.

    Args:
        execution_text: Raw execution record text.

    Returns:
        The joined outcome fields, or ``None`` when the record is not JSON — in which case it
        is a CLI-level failure message with no tool results in it, and searching it whole is
        both safe and necessary (a rejected ``--json-schema`` arrives exactly that way).
    """

    events = _parse_events(execution_text)
    if events is None:
        return None

    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for field in OUTCOME_FIELDS:
            parts.extend(_strings_in(event.get(field)))
    return "\n".join(parts)


def _strings_in(value: object, depth: int = 0) -> list[str]:
    """Collect the string leaves of an outcome field, however it is nested.

    Anthropic-shaped errors arrive as an object — ``{"type": ..., "message": ...}`` — so a
    string-only reading would skip the very field it exists to look at and classify a real
    provider error as "no recognisable error". Recursion is bounded and applies **only** to
    the fields in :data:`OUTCOME_FIELDS`; tool results live under ``message``/``content``,
    which are not among them, so nothing the reviewer read is reachable from here.

    Args:
        value: An outcome field's value.
        depth: Current recursion depth.

    Returns:
        Every string leaf, each capped at :data:`OUTCOME_FIELD_CHARS`.
    """

    if depth > 3:
        return []
    if isinstance(value, str):
        return [value[:OUTCOME_FIELD_CHARS]] if value else []
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings_in(item, depth + 1)]
    if isinstance(value, list):
        return [s for item in value for s in _strings_in(item, depth + 1)]
    return []


def _first_match(patterns: tuple[str, ...], haystack: str) -> str | None:
    """Return the first pattern match in a haystack.

    Args:
        patterns: Regular expressions to try in order.
        haystack: Lowercased text to search.

    Returns:
        The matched text, or None when nothing matched.
    """

    for pattern in patterns:
        match = re.search(pattern, haystack)
        if match:
            return match.group(0)
    return None


def classify(
    execution_text: str,
    *,
    record_present: bool = True,
    elapsed_seconds: int | None = None,
) -> tuple[str, str]:
    """Classify a failed attempt as exhausted or fatal.

    Args:
        execution_text: Raw execution record text.
        record_present: False when the action produced no execution record at
            all.
        elapsed_seconds: Wall clock of the attempt, or None when it was not
            measured. Consulted **only** when no execution record exists: a
            record means the run reached the model and said something, and what
            it said outranks how long it took.

    Returns:
        A ``(status, reason)`` pair where status is ``"exhausted"`` (the
        provider could not serve it; re-running may work) or ``"fatal"`` (the
        workflow is wrong; re-running will not). Both fail the job.
    """

    # 🔴 upstream. Two opposite failures arrive with no execution record, because
    # the action writes the file only AFTER a call returns: a setup fault that
    # never reached the model, and a model call killed by `API_TIMEOUT_MS`
    # having burned the whole budget. Until this ticket both were called
    # `fatal`, which sends an engineer to edit a workflow that is correct --
    # measured at 20m35s on PR #397 and again on PR #345, each refuted by a
    # plain re-run passing in about seven minutes.
    #
    # Duration is what separates them, and the separation is measured: the setup
    # faults under this reason came in at 40s and 43s, the runs that reached the
    # model at 1242-1524s (see `retry_verdict`). `timed_out` is that test, and
    # it refuses to answer when the measurement is missing -- a verdict of
    # "transient" asserted on no evidence would be this ticket's own complaint
    # pointing the other way.
    if not record_present:
        if timed_out(elapsed_seconds):
            return (
                "exhausted",
                f"the attempt ran {elapsed_seconds}s, long enough to have "
                f"reached the model and consumed the whole "
                f"{call_budget_seconds()}s API_TIMEOUT_MS budget for one call, "
                "and the action writes its execution record only after a call "
                "returns. A misconfiguration fails in seconds, so this is the "
                "provider rather than the workflow, and a re-run is the fix",
            )
        return "fatal", "no execution record; Claude never reached the model"

    # Scoped to the record's own outcome fields when it is JSON, so that text the model merely
    # READ cannot vote on why the run failed. A record that is not JSON is a CLI-level message
    # with no tool results in it, and is searched whole -- which is how a rejected
    # `--json-schema` is still caught.
    scoped = _outcome_text(execution_text)
    haystack = (execution_text if scoped is None else scoped).lower()

    # Fatal first: a rejected schema can coexist with other noise in the record,
    # and spending the remaining providers on it is pure waste.
    hit = _first_match(FATAL_PATTERNS, haystack)
    if hit:
        return "fatal", f"workflow-level failure: {hit!r}"

    hit = _first_match(QUOTA_PATTERNS, haystack)
    if hit:
        return "exhausted", f"provider quota exhausted: {hit!r}"

    hit = _first_match(CREDENTIAL_PATTERNS, haystack)
    if hit:
        return "exhausted", f"provider rejected the credentials or model: {hit!r}"

    hit = _first_match(EXHAUSTED_PATTERNS, haystack)
    if hit:
        return "exhausted", f"provider unavailable: {hit!r}"

    # The model drove the whole review and then could not express it in the
    # schema. Named separately because the fallthrough below reads "no
    # recognisable error" -- and this error is not only recognisable, it is the
    # one the CLI puts in the record verbatim.
    #
    # Measured on PR #236, run 31022316901: 83 turns, $9.09 billed, then
    # `error_max_structured_output_retries`. It landed in the fallthrough and the
    # operator was told to top up a balance that had just been spent doing the
    # work. Two runs on the same branch, the same schema and the same key had
    # succeeded within the hour, which is the evidence that re-running is the
    # right response and topping up is not.
    #
    # `exhausted` rather than `fatal` is deliberate and unchanged: the run may
    # well succeed next time, and a `fatal` verdict says the opposite. Only the
    # REASON was wrong.
    hit = _first_match(STRUCTURED_OUTPUT_PATTERNS, haystack)
    if hit:
        return (
            "exhausted",
            "the model completed the review but could not return output matching "
            f"--json-schema ({hit!r}); it ran to completion and was billed, so this "
            "is not a spent balance. Re-run: the same schema and key have "
            "succeeded on retries. If it recurs, the lever is the schema or the "
            "model, never the balance",
        )

    # Claude ran and returned nothing recognisable. This is where a model that
    # cannot drive structured output lands in a way we have no name for, so it is
    # provider-specific: the next tier may well handle the schema correctly.
    return "exhausted", "ran but returned no payload and no recognisable error"


#: The bridge-launch failures kitty NAMES, in the order they must be tested.
#:
#: ⚠️ **Egress before proxy-auth, and the order is load-bearing.** Measured by the
#: sibling repository against the same gateway: a wrong password answers **407**, a
#: correct password from an address the gateway does not allow answers **403**. The
#: 403 message also carries the word "proxy", so a proxy-auth rule matching first
#: would send an operator to re-copy a credential that is already right.
#:
#: 🔴 **A table rather than three `if` blocks, because upstream needs the membership
#: question answered separately from the verdict.** :func:`classify_bridge` returns a
#: `fatal` for ANY non-empty stderr — its last branch is a catch-all — so "the bridge
#: classifier produced a verdict" is true on almost every failed run and cannot stand
#: in for "kitty named a cause". Asking the two questions of one ordered table is what
#: keeps them from drifting apart.
BRIDGE_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        BRIDGE_EGRESS_PATTERNS,
        "the egress gateway refused this runner (403). The credential is "
        "not the problem -- a wrong password answers 407. The gateway is in "
        "IP-allowlist mode and the runner's address is not on it: add it in "
        "the gateway dashboard, or switch that gateway user to "
        "password-only auth. Nothing in this repository can fix it",
    ),
    (
        BRIDGE_PROXY_AUTH_PATTERNS,
        "the egress gateway rejected the proxy password (407). Re-copy the "
        "working credentials.json into KITTY_CREDENTIALS_JSON -- this is "
        "the credential case, distinct from the 403 allowlist one",
    ),
    (
        BRIDGE_PROFILE_PATTERNS,
        "kitty could not resolve a profile or its credential, so it never "
        "reached a provider. Check KITTY_PROFILES_JSON and "
        "KITTY_CREDENTIALS_JSON travel together and name the same profile",
    ),
)


def bridge_named_a_cause(bridge_text: str) -> bool:
    """Report whether kitty's stderr names one of the failures it knows.

    Distinct from ``classify_bridge(...) is not None``, and the difference is
    the whole point: that call answers "did anything come back", and its last
    branch is a catch-all that fires on any non-empty stderr. Kitty writes
    ordinary chatter on every healthy run, so it is true nearly always.

    This answers the narrower question upstream needs — *does an operator have
    something specific to change* — which is what makes a second launch
    pointless rather than merely unlucky.

    Args:
        bridge_text: Contents of the wrapper's teed stderr log, possibly empty.

    Returns:
        True only when one of :data:`BRIDGE_FAMILIES` matches.
    """

    haystack = bridge_text.lower()
    return any(_first_match(patterns, haystack) for patterns, _ in BRIDGE_FAMILIES)


def classify_bridge(bridge_text: str) -> tuple[str, str] | None:
    """Classify a Kitty Bridge launch failure from the wrapper's stderr.

    Runs before :func:`classify` because when the bridge does not launch there
    is no execution record to classify at all, and the record-absent branch's
    advice ("check the settings list") is wrong for every case below.

    Args:
        bridge_text: Contents of the wrapper's teed stderr log, possibly empty.

    Returns:
        A ``(status, reason)`` pair when kitty named a failure, otherwise
        ``None`` so the caller falls through to the execution record. All three
        outcomes are ``fatal``: each needs an operator to change something, and
        re-running changes nothing.
    """

    haystack = bridge_text.lower()
    if not haystack.strip():
        return None

    if bridge_named_a_cause(bridge_text):
        for patterns, reason in BRIDGE_FAMILIES:
            if _first_match(patterns, haystack):
                return "fatal", reason

    return "fatal", (
        "kitty wrote to stderr and Claude produced no usable result. Its "
        "message is under 'kitty bridge stderr' below -- that stream is the "
        "only place a bridge launch failure appears"
    )


def call_budget_seconds() -> int | None:
    """Read the workflow's own bound on a single model call.

    Deliberately read from ``API_TIMEOUT_MS`` rather than restated as a
    constant. It is the CLI's read timeout, declared once at the top of
    ``claude-code-review.yml``; a number copied here would be right on the day
    it was written and wrong the first time that one moved.

    Returns:
        The budget in whole seconds, or None when the variable is absent or not
        a number — in which case :func:`retry_verdict` refuses to call anything
        fast, which is the safe direction.
    """

    raw = os.environ.get("API_TIMEOUT_MS", "").strip()
    if not raw.isdigit():
        return None
    return int(raw) // 1000


def timed_out(elapsed_seconds: int | None) -> bool:
    """Report whether an attempt consumed a whole model call's budget.

    🔴 **upstream. One predicate, read by both halves of this module, because
    they were about to encode the same threshold in opposite directions.**
    :func:`classify` asks it to decide what to CALL the failure and
    :func:`retry_verdict` to decide whether to SPEND on it again — and the two
    answers differ, which is exactly why the test behind them must not be
    duplicated. A second copy would eventually disagree with the first, and the
    disagreement would read as a policy decision rather than as drift.

    ⚠️ **This is a proxy, and the two directions are not equally sound.**
    ``elapsed_seconds`` is the whole attempt's wall clock — stamped before the
    action's own setup — while ``API_TIMEOUT_MS`` bounds a single *call*. So
    "quicker than one call's budget" genuinely entails "no call ran to that
    budget", which is the contrapositive upstream relied on; "as long as the
    budget" only makes a completed call *possible*. What carries the second
    direction is not the clock alone but what it excludes: a bad endpoint or an
    unroutable model fails in seconds, so nothing that fails slowly is one.

    ``>=`` rather than ``>`` for a reason worth stating, because it is not the
    obvious one: it makes this predicate and :func:`retry_verdict` exactly
    complementary. At ``budget - 1`` an attempt is ``fatal`` and retried; at
    ``budget`` it is ``exhausted`` and not. No elapsed value is both "not a
    timeout" and "not worth retrying", so no run falls between the two rules.
    The retry decision itself is identical under either comparison — only the
    message moves, and only by one second.

    Args:
        elapsed_seconds: Wall clock of the attempt, or None when it was not
            measured.

    Returns:
        True only when the attempt was measured, the budget is readable, and the
        attempt reached it. Unmeasured is False — a claim needs its evidence,
        and every caller's safe direction is to withhold the claim.
    """

    # ⚠️ Written as one expression rather than as the guard-then-compare shape
    # `retry_verdict` uses, and deliberately. Spelled the obvious way this
    # function's first two lines were CHARACTER-IDENTICAL to that function's,
    # which is not a style question: `check_sweep_anchors` counts substring
    # occurrences over the whole file, so the upstream sweep's one-line anchor on
    # them began matching twice and scored stale. `str.replace(old, new, 1)` is
    # boundary-blind, so a longer anchor would have been the second-best fix --
    # having no duplicate to disambiguate is the first.
    budget = call_budget_seconds()
    return (
        budget is not None and elapsed_seconds is not None and elapsed_seconds >= budget
    )


def retry_verdict(
    status: str,
    *,
    record_present: bool,
    cause_named: bool,
    elapsed_seconds: int | None = None,
    timed_out_attempt: bool = False,
) -> bool:
    """Decide whether this attempt is worth making a second time.

    **The question is cost, and upstream is where cost and the status name came
    apart.** upstream wired one automatic retry and gated it on ``exhausted``,
    recording that ``fatal`` must never retry because re-running is *"guaranteed
    waste at roughly $5"*. That reasoning is right, and it does not cover the
    whole of ``fatal``: a run that never reached the model billed nothing, and
    that is the shape upstream observed clearing on a hand re-run — attempt 2 of
    ``13f9e4fc`` failed that way and attempt 3 passed on an unchanged input.

    🔴 **"No execution record" does NOT mean "never reached the model", and
    assuming it did was this ticket's second wrong diagnosis.** The action
    writes its execution file only after the run returns, so a call killed by a
    cap loses the file having burned the whole budget. Measured in this
    repository: upstream recorded passes at 9m23s and kills at 12m12s that *"write
    no execution file"*, and run 32134116453 spent **1267s** before reporting
    exactly this reason.

    **What separates the two is elapsed time, and the separation is measured.**
    Six failed review runs were opened and classified by the reason they
    actually reported. Those reporting *no execution record* fall in two groups
    with nothing between them:

    * **40s** (run 32241396858, a missing ``unzip``) and **43s** (32052769986,
      a prompt over the argument limit) — the setup faults;
    * **1242s** (32130657535), **1267s** (32134116453), **1311s** (32054274461)
      and **1524s** (32056710602) — runs that reached the model.

    The threshold is :func:`call_budget_seconds` — the workflow's own
    ``API_TIMEOUT_MS``, today 480s — which lies inside that gap and is a number
    the workflow already declares rather than one chosen here. A failure quicker
    than a single model call's budget cannot be a model call that ran.

    ⚠️ **Six runs, individually checked — not the whole failure population, and
    an earlier version of this paragraph claimed otherwise.** It said "every
    failed review run in the last 200" was bimodal at 14-120s and 1242-1524s.
    That was the *duration* spread of all failures regardless of reason, and it
    is not this class: failures at 281s and 374s sit between the two groups, and
    both are ``exhausted`` with an execution record present. The correction
    matters because the sample is small and honest about being small, where the
    original read as a census.

    ⚠️ **A named bridge failure is excluded even though it is just as cheap.**
    Kitty's egress 403, its proxy 407 and an unresolvable profile all arrive with
    an empty record within seconds, but each names a setting an operator must
    change, and a second launch cannot supply one. Note that this is
    :func:`bridge_named_a_cause`, **not** ``classify_bridge(...) is not None``:
    that call has a catch-all last branch and is true for any non-empty stderr,
    which kitty writes on every run.

    ⚠️ **This is a proxy for billing, not a measurement of it.**
    ``claude-code-action@v1`` exposes ``conclusion``, ``execution_file``,
    ``branch_name``, ``github_token`` and ``structured_output`` — no turn count
    and no cost — so nothing here can read what a run actually spent. Two
    consequences are accepted rather than hidden: a deterministic fast failure
    (a malformed schema, an over-long prompt) is retried and fails identically,
    costing seconds of runner time and no model billing; and a model call that
    somehow fails inside the budget is retried at full price, which is the same
    trade upstream accepted for ``exhausted``.

    Args:
        status: The classification, one of ``ok``, ``exhausted`` or ``fatal``.
        record_present: Whether the action produced an execution record.
        cause_named: Whether kitty's stderr named one of the failures it knows.
        elapsed_seconds: Wall clock of this attempt, or None when it was not
            measured — in which case a ``fatal`` is never retried.
        timed_out_attempt: Whether :func:`classify` found this attempt to have
            burned a whole call budget with nothing to show for it. upstream:
            passed in rather than recomputed, because the caller knows things
            this function does not — chiefly whether a findings payload came
            back, which is evidence the provider answered however long it took.

    Returns:
        True when a second attempt is worth its cost.
    """

    if status == "ok":
        return False
    if status == "exhausted":
        # 🔴 upstream put one attempt in this bucket that must NOT be retried,
        # and it is the expensive one this function was written to refuse. A
        # timed-out attempt with no execution record is now called `exhausted`
        # because that is the honest thing to tell a human -- the provider did
        # not answer and a re-run is the fix. It is still the 1267s attempt
        # upstream measured, which burned a whole call budget and was billed for
        # it, so a second one is spent on the run that already cost the most.
        #
        # Re-classifying an attempt does not re-price it. That is the whole
        # reason the verdict and the retry are separate functions: the status
        # routes a human, `retryable` spends money, and reading one off the
        # other is the mistake upstream was filed over.
        #
        # ⚠️ `timed_out_attempt` is the CALLER's finding, not a second derivation
        # of it. A first version of this recomputed `timed_out(elapsed_seconds)`
        # here, and design review caught what that costs: `main` classifies a
        # schema-valid-but-empty payload as `exhausted` from the payload alone,
        # so an empty review that arrived slowly and without a record satisfied
        # a locally-recomputed "timed out" and silently lost upstream's retry --
        # while the diagnostic beside it printed "the provider did not answer in
        # time" about a provider whose answer was in the file. One finding,
        # derived once, consumed everywhere, is the only shape in which those
        # two cannot disagree.
        return not timed_out_attempt

    # `fatal`. Retry only the sub-case that named nothing an operator could fix
    # and finished too quickly to have been a model call.
    #
    # ⚠️ **The duration test below is now a BACKSTOP, and the rows that drive it
    # must not be deleted as unreachable.** Since upstream, `classify` routes a
    # slow record-absent attempt to `exhausted`, so most slow values never get
    # here -- but a bridge failure does: `classify_bridge` returns `fatal` for
    # any non-empty kitty stderr whatever the clock. The unit rows in
    # `test_the_verdict_function_is_not_hard_wired_either_way` are what kill
    # mutants 1-3 of the upstream sweep, and they are cheap.
    if record_present or cause_named:
        return False
    budget = call_budget_seconds()
    if budget is None or elapsed_seconds is None:
        return False
    return elapsed_seconds < budget


def _write_diagnostic(
    path: str,
    *,
    tier: str,
    status: str,
    reason: str,
    retryable: bool,
    record_present: bool,
    execution_text: str,
    bridge_text: str = "",
    elapsed_seconds: int | None = None,
    timed_out_attempt: bool = False,
    payload_present: bool = False,
) -> None:
    """Write a human-readable diagnostic for the workflow to surface.

    Appends rather than overwrites. That mattered most when several tiers each
    wrote a section; with one provider it still keeps a re-run from erasing the
    record of the attempt before it.

    Args:
        path: Destination file.
        tier: Human-readable provider name, for example "DeepSeek".
        status: Classification result.
        reason: Short explanation of the classification.
        retryable: Whether this attempt will be made again automatically.
        record_present: Whether an execution record existed.
        execution_text: Raw execution record text.
        bridge_text: Kitty's teed stderr, empty when the wrapper wrote none.
        elapsed_seconds: Wall clock of the attempt, or None when unmeasured.
            upstream: the record-absent branch has three bodies rather than one,
            and this separates the two that are not the timeout.
        timed_out_attempt: :func:`classify`'s finding that the attempt burned a
            whole call budget. Passed in rather than recomputed — see
            :func:`retry_verdict`, where recomputing it printed a timeout
            paragraph four lines under ``retryable: true``.
        payload_present: Whether a findings payload came back. When one did, the
            run produced something to read and none of the record-absent bodies
            applies, however long it took.
    """

    # `retryable` is printed rather than left to be re-derived from `status`.
    # Re-deriving it is what upstream found people doing, and getting wrong: the
    # ticket's author read `fatal` as "deterministic", went looking for a
    # deterministic cause, and filed a diagnosis that a single re-run refuted.
    #
    # ⚠️ It says whether the failure is worth ANOTHER attempt, not whether one
    # happened -- the attempt COUNT lives in the notice and the job summary.
    # Both attempts are stamped and both pass their elapsed time, so the same
    # failure shape gets the same verdict in either section; an unstamped retry
    # would have printed `false` under attempt 2 for a shape attempt 1 called
    # `true`, and the difference would have been "not measured" wearing the
    # costume of a policy decision.
    verdict = (
        "true (worth another attempt)"
        if retryable
        else "false (another attempt could not help)"
    )
    lines = [
        "",
        f"=== tier {tier} ===",
        f"status: {status}",
        f"retryable: {verdict}",
        f"reason: {reason}",
        "",
    ]

    # Placed ABOVE the record, not below it. When kitty fails to launch the
    # record is empty, so a reader who has to scroll past an empty record to
    # reach the only text that says what happened will conclude there is nothing
    # to read. Tail-bounded for the same reason the record is: this stream can
    # carry a stack trace per retry.
    if bridge_text.strip():
        tail = bridge_text.strip().splitlines()[-40:]
        lines += ["--- kitty bridge stderr (tail) ---", *tail, ""]

    # 🔴 Scoped exactly as `classify` is, and this is the half that did the damage. The first
    # version of the upstream fix corrected the VERDICT and left this branch reading the whole
    # record -- so a run whose reviewer happened to read a file containing `quota` still
    # printed "top up the balance" under an `exhausted` heading. The wrong verdict is
    # confusing; the wrong instruction costs money.
    scoped = _outcome_text(execution_text)
    evidence = execution_text if scoped is None else scoped

    # A missing execution record is the least self-explanatory failure, so spell
    # out what to check rather than leaving an empty log.
    #
    # 🔴 upstream split this in three, and the split has to happen HERE as well as
    # in `classify`. Moving the one-word verdict and leaving this branch alone
    # would have printed "check these settings" underneath a verdict that says
    # the settings are fine -- the same defect one layer down, and the shape
    # upstream already had to fix once when the verdict was corrected and the
    # evidence beneath it was not.
    #
    # ⚠️ `payload_present` gates the whole block, and that is a defect this
    # ticket sharpened rather than introduced. A schema-valid-but-empty review
    # can arrive with no execution record, and this advice was printed for it --
    # tolerable while the opening line hedged ("that USUALLY means"), false as
    # soon as upstream made it state a finding. When a payload came back there is
    # something to read and the reason already explains it, so none of the three
    # bodies below applies.
    if not record_present and not payload_present:
        # 🔴 TWO QUESTIONS, ANSWERED FROM TWO DIFFERENT FACTS, and conflating them
        # is what design review caught. The OPENING reports the clock, which is
        # true on every path into this block. The ADVICE follows the
        # CLASSIFICATION, which knows things the clock does not -- chiefly
        # whether kitty named a cause an operator has to go and change.
        #
        # They come apart on exactly one reachable state: a named bridge failure
        # on a slow attempt. The clock says the run was long; the verdict says an
        # egress allowlist or a proxy password is what to fix. Printing "this is
        # the provider, re-run" there would send an operator away from the one
        # setting that would fix it.
        # ⚠️ An unreadable budget is as unmeasured as an unstamped attempt, and
        # a first version guarded only the second. `call_budget_seconds` returns
        # None when `API_TIMEOUT_MS` is absent or not a digit string, which makes
        # `timed_out` False for an attempt of ANY length -- so the confident
        # "it was not a timeout" opening was printed over a run that may have
        # taken twenty minutes, reached through the one input this ticket added.
        if elapsed_seconds is None or call_budget_seconds() is None:
            lines += [UNMEASURED_OPENING]
        elif timed_out(elapsed_seconds):
            lines += [REACHED_THE_MODEL_OPENING]
        else:
            lines += [INSIDE_THE_BUDGET_OPENING]
        lines += ["", TIMED_OUT_DIAGNOSIS if timed_out_attempt else NO_RUN_ADVICE, ""]
    elif re.search(CONTEXT_MANAGEMENT_REFUSAL, evidence, re.I):
        # upstream. Placed above the quota branch so a refusal that happens to
        # carry a billing word cannot be read as a spent balance.
        #
        # ⚠️ It does NOT pre-empt the record-absent bodies -- those are the `if`
        # reachable only when no execution record exists, which is the one shape
        # this branch is `elif`-ed out of. A first version of this comment
        # claimed it beat those four settings; it does not, and the true reason
        # the branch is needed is simpler: with a record present there was no
        # branch at all, so the run printed its verdict and no guidance.
        #
        # Claude Code sends Anthropic's context-management beta at the protocol
        # level. OpenRouter serves it only for Anthropic-family models and
        # rejects it for every other one; a reporter upstream stripped the beta
        # header, the query parameter and the `betas` array and was still
        # refused.
        lines += [
            "The gateway refused the request because Claude Code asked for "
            "Anthropic's context-management feature and the configured model "
            "cannot serve it. This is not a defect in this pull request and "
            "re-running unchanged will not help.",
            "",
            "OpenRouter serves that feature only for Anthropic-family models. "
            "The reliable fix is to point the active kitty profile at an "
            "`anthropic/*` model id -- the combination OpenRouter guarantees "
            "for Claude Code -- or to switch the profile to a provider whose "
            "own endpoint accepts the model you want. Both are edits to the "
            "KITTY_PROFILES_JSON repository variable, the profile being the "
            "only thing that selects a model since the migration to Kitty "
            "Bridge -- this workflow passes no --model flag at all.",
            "",
            "Worth one attempt first, because it costs a re-run: Claude Code "
            "documents CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 for exactly "
            "this symptom against a third-party gateway. It is NOT a "
            "guarantee -- it is reported not to suppress every beta header, "
            "and upstream stripping attempts did not clear this one -- so "
            "treat a second identical failure as confirmation that the model "
            "is the knob.",
            "",
        ]
    elif any(re.search(p, evidence, re.I) for p in QUOTA_PATTERNS):
        # Quota exhaustion is not a defect in the change under review, and it is
        # now the most likely reason this check is red. Spell out what to do
        # rather than leaving a reader to infer it from the record tail.
        #
        # Keyed off the whole QUOTA_PATTERNS set rather than one provider's
        # wording. An earlier version upstream tested for a single phrase, and
        # when the provider behind it changed the branch could never fire -- so
        # the failure an operator hits most often printed no guidance at all.
        reset = re.search(r"limit will reset at ([^\]\"]+)", evidence, re.I)
        lines += [
            "The provider could not serve the request because its quota or "
            "balance is spent. OpenRouter, the gateway the current kitty "
            "profile points at, bills from a prepaid credit "
            "balance and has no overage billing, so calls fail until it is "
            "topped up." + (f" Resets at {reset.group(1).strip()}." if reset else ""),
            "",
            "Nothing is wrong with this pull request, but the review did not "
            "run, so this check fails. Top up the balance and re-run.",
            "",
        ]

    lines += [
        "--- execution record (tail) ---",
        "\n".join(execution_text.splitlines()[-60:]) or "(no execution record)",
    ]

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    """Classify one attempt and write its status for the workflow.

    Returns:
        Always 0. The workflow decides whether to fail based on the emitted
        status, so a classification result is never itself a build error.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        required=True,
        help='Provider label, for example "DeepSeek".',
    )
    parser.add_argument("--github-output", required=True)
    parser.add_argument("--structured-output-out", required=True)
    parser.add_argument("--diagnostic-out", required=True)
    parser.add_argument(
        "--elapsed-seconds",
        type=int,
        default=None,
        help=(
            "Wall clock of the attempt just interpreted. Absent means unmeasured, "
            "and a `fatal` is then never retried -- see `retry_verdict`."
        ),
    )
    args = parser.parse_args()

    raw_output = os.environ.get("CLAUDE_STRUCTURED_OUTPUT", "")
    execution_path = os.environ.get("CLAUDE_EXECUTION_FILE")
    execution_text = _read_execution_record(execution_path)
    record_present = bool(execution_text.strip())

    # Kitty's launch stderr, teed by the wrapper. Read defensively: a missing
    # file is the normal case on a healthy run, and an unreadable one must not
    # turn a classifiable failure into a stack trace.
    bridge_text = ""
    bridge_path = os.environ.get("KITTY_STDERR_FILE")
    if bridge_path:
        try:
            bridge_text = Path(bridge_path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            bridge_text = ""

    payload = _extract_structured_output(raw_output, execution_text)

    # Whether `classify_bridge` supplied the verdict. Initialised here because
    # the retry decision below needs it on every path, and only one branch sets
    # it -- an unset name would make a produced review look like a named bridge
    # failure, which is the direction that silently suppresses a retry.
    cause_named = False

    # upstream, initialised for the same reason. Only the record-based branch can
    # find a timeout, and the two branches that skip it -- a produced review, and
    # a payload that was empty -- are both evidence the provider answered.
    attempt_timed_out = False

    if payload is not None and is_substantive(payload):
        out_path = Path(args.structured_output_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        status = "ok"
        reason = f"valid payload with {len(payload.get('findings', []))} finding(s)"
    elif payload is not None:
        # Structurally valid, substantively empty. See `is_substantive`.
        status = "exhausted"
        # The notes rule is reported first because it is the stricter one: it
        # can reject a payload that carries findings, where the floor never
        # can, so a payload tripping both is better described by it. Reporting
        # the floor instead would send a reader looking for a short summary a
        # review with five findings does not have.
        #
        # 🔴 Fixed strings only. `reason` becomes one `reason=` line in
        # `$GITHUB_OUTPUT`, so interpolating model-controlled text here could
        # close the line early and forge a later key. The floor branch below
        # interpolates a length, which is a number.
        if not _conversation_notes(payload):
            reason = (
                "the provider returned a review whose conversation_notes is blank, "
                "so nothing records whether the pull request discussion was read. "
                "Re-run."
            )
        else:
            reason = (
                "the provider returned a schema-valid but empty review "
                f"(no findings; {len(str(payload.get('summary', '')).strip())}-character "
                f"summary, under the {MINIMUM_EMPTY_REVIEW_SUMMARY} required when nothing "
                "was found). Re-run."
            )
    else:
        # 🔴 The bridge is consulted ONLY when there is no execution record, and
        # the narrowness is deliberate. Kitty writes ordinary chatter to stderr
        # on every healthy run, so a bridge verdict that could fire whenever
        # stderr was non-empty would override a correct record-based
        # classification -- it would have relabelled PR #401's
        # `error_max_structured_output_retries` (a full record, 224s of model
        # time, correctly classified) as a launch failure.
        #
        # A record existing means the run reached the model, which means the
        # bridge came up. So the two sources never both have standing, and the
        # record wins wherever it exists.
        #
        # 🔴 upstream added a second thing that outranks the bridge, and only the
        # CATCH-ALL half of it. `classify_bridge`'s last branch fires on any
        # non-blank stderr, and kitty writes ordinary chatter on every healthy
        # run -- so "kitty said something" would have outranked a MEASURED
        # timeout and called it a misconfiguration again, which is this ticket's
        # whole subject. Chatter is not evidence. A NAMED cause still wins at any
        # duration: an egress 403 or a proxy 407 needs a setting changed, and
        # that stays true however long the attempt took.
        bridge_speaks = not record_present and (
            bridge_named_a_cause(bridge_text) or not timed_out(args.elapsed_seconds)
        )
        bridge_verdict = classify_bridge(bridge_text) if bridge_speaks else None
        if bridge_verdict is not None:
            status, reason = bridge_verdict
            # NOT `bridge_verdict is not None`: that is true for any non-empty
            # stderr, which kitty writes on every run. See `bridge_named_a_cause`.
            cause_named = bridge_named_a_cause(bridge_text)
        else:
            status, reason = classify(
                execution_text,
                record_present=record_present,
                elapsed_seconds=args.elapsed_seconds,
            )
            # upstream. `classify` is the ONE place that decides this, and every
            # consumer below reads its finding rather than re-deriving it.
            # Reached only when no payload came back, so a schema-valid-but-empty
            # review -- which is `exhausted` from the payload alone, and is
            # evidence the provider answered -- cannot land here however slow it
            # was. With a record present, `exhausted` means the record said so,
            # which is a provider that answered too.
            #
            # ⚠️ **The invariant this reads off: a record-absent `exhausted` is
            # the timeout and nothing else.** `classify`'s record-absent branch
            # returns exactly two verdicts and only one of them is `exhausted`.
            # A second record-absent `exhausted` added later would break this
            # silently, so give `classify` a third return value rather than
            # widening that branch.
            attempt_timed_out = status == "exhausted" and not record_present

    retryable = retry_verdict(
        status,
        record_present=record_present,
        cause_named=cause_named,
        elapsed_seconds=args.elapsed_seconds,
        timed_out_attempt=attempt_timed_out,
    )

    _write_diagnostic(
        args.diagnostic_out,
        tier=args.tier,
        status=status,
        reason=reason,
        retryable=retryable,
        record_present=record_present,
        execution_text=execution_text,
        bridge_text=bridge_text,
        elapsed_seconds=args.elapsed_seconds,
        timed_out_attempt=attempt_timed_out,
        payload_present=payload is not None,
    )

    with open(args.github_output, "a", encoding="utf-8") as handle:
        handle.write(f"status={status}\n")
        handle.write(f"reason={reason}\n")
        handle.write(f"retryable={'true' if retryable else 'false'}\n")

    print(f"tier {args.tier}: {status} (retryable: {retryable}) -- {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
