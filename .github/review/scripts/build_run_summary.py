"""Render the job summary shown at the top of the workflow run page.

A failed attempt leaves annotations behind that say nothing about whether the
run mattered. ``claude-code-action`` writes two ``::error::`` lines and the
runner adds a third for the non-zero exit, and ``continue-on-error`` suppresses
none of them.

Left unexplained, a run page that opens with red crosses teaches people to
ignore this workflow, which is worse than the workflow not existing.

With one provider and a failed review failing the job, the case to guard
against is a red run whose annotations do not distinguish "the provider was out
of quota" from "the workflow is broken" -- two outcomes fixed by different
people.

The summary below is written to ``$GITHUB_STEP_SUMMARY``, which GitHub renders
above the step list, so the first thing a reader sees says what actually
happened and what to do about it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# Documentation, not a live link: nobody should have to rediscover why the
# annotation list is longer and less specific than the actual failure.
ANNOTATION_NOTE = (
    "**The red annotations above are not the diagnosis.** A provider that could "
    "not serve the request leaves three of them — two from the action and one "
    "from the runner — and GitHub offers no way to suppress an annotation "
    "raised inside a third-party action. The outcome of this run is the table "
    "above, not the annotation list."
)


@dataclass(frozen=True)
class Tier:
    """One provider attempt.

    Attributes:
        name: Human-readable provider label, for example ``"DeepSeek"``.
        available: Tri-state, because three different things look like "did not
            run" and they are not equally serious. ``"true"`` means the provider
            was configured and attempted; ``"false"`` means the bridge was not
            usable; empty means the step did not run at all. The empty case
            cannot arise with a single unconditional Configure step, and is kept
            so this renderer stays correct if a second provider is ever added
            behind an ``if:``.

            🔴 **``"false"`` widened with upstream and this docstring is where the
            widening is recorded.** It read *"its Configure step found no key or
            model"*, and the workflow now ANDs the egress verdict in, so the same
            value also covers *kitty resolved no egress gateway* -- a run whose
            key and model are perfectly fine. Which one it was cannot be
            recovered from this field, so :func:`build` takes the gate's own
            CAUSE separately rather than guessing; see the comment there for what
            guessing cost, twice.
        status: Classification from :mod:`interpret_claude_result`, empty when
            the provider was never attempted.
        reason: That classification's short explanation.
    """

    name: str
    available: str
    status: str
    reason: str

    @property
    def attempted(self) -> bool:
        """Whether this tier actually invoked the review action.

        Returns:
            True only for a configured tier that was reached; these are the
            tiers that can have left annotations behind.
        """

        return self.available == "true"


def parse_tier(spec: str) -> Tier:
    """Parse a ``name|available|status|reason`` argument.

    Args:
        spec: Pipe-separated tier description from the command line.

    Returns:
        The parsed :class:`Tier`.
    """

    # Split at most three times: a provider's reason text may itself contain a
    # pipe, and it is the last field, so it keeps whatever it contains.
    parts = spec.split("|", 3)
    parts += [""] * (4 - len(parts))
    return Tier(
        parts[0].strip(), parts[1].strip().lower(), parts[2].strip(), parts[3].strip()
    )


def _cell(text: str) -> str:
    """Make text safe to place in a Markdown table cell.

    Args:
        text: Raw text, possibly containing pipes or newlines.

    Returns:
        The text with table-breaking characters neutralised.
    """

    return text.replace("|", "\\|").replace("\n", " ").strip() or "—"


def _headline(result: str, provider: str) -> list[str]:
    """Build the opening lines stating what the run achieved.

    Args:
        result: Resolved run outcome: ``ok``, ``exhausted`` or ``fatal``.
        provider: Winning tier label, empty unless the result is ``ok``.

    Returns:
        Markdown lines.
    """

    if result == "ok":
        return [
            f"## Review posted — {provider or 'unknown provider'}",
            "",
            "The pull request was reviewed and the findings are on the "
            "*Files changed* tab.",
        ]
    if result == "exhausted":
        return [
            "## No review this run — the provider was unavailable",
            "",
            "**This check fails**, because the pull request has not been "
            "reviewed. Nothing is wrong with the change itself: the provider "
            "could not serve the request — quota, credentials, or a transient "
            "error. Top up or wait, then re-run.",
            "",
            "*There is one provider and no fallback, so an unavailable provider "
            "means the change would otherwise merge unreviewed. That is why "
            "this outcome is red rather than green.*",
        ]
    # 🔴 upstream. This ended "so re-running will not clear it" -- word for word
    # the claim removed from the pull request comment and the `::error::` line.
    # §14.3 treats the three as ONE voice, and this is the surface an operator
    # opens first, so fixing the other two alone would have left the run
    # contradicting itself and put the retired verdict where it sounds most
    # authoritative.
    #
    # 🔴 upstream removed the caveat that replaced it, from all three surfaces
    # together. That paragraph asked the reader to compare the run's duration
    # against a normal review by hand, because the classifier could not. It can:
    # `interpret_claude_result.classify` reads the attempt's elapsed time and a
    # timed-out one is now `exhausted`, so a reader who reaches THIS branch has
    # already been told, by measurement, that the run was not merely slow.
    # Leaving the paragraph would re-open the doubt the measurement closes.
    return [
        "## Review failed — the workflow needs fixing",
        "",
        "The failure is in the **workflow configuration**, not in the change "
        "under review.",
    ]


def build(
    result: str,
    provider: str,
    tiers: list[Tier],
    egress_cause: str = "",
    configure_reason: str = "",
) -> str:
    """Build the job summary in Markdown.

    Args:
        result: Resolved run outcome: ``ok``, ``exhausted`` or ``fatal``.
        provider: Winning tier label, empty unless the result is ``ok``.
        tiers: Every provider attempt, whether or not it ran.
        egress_cause: Why the egress gate refused, verbatim from
            ``steps.egress.outputs.cause``. Three values reach here:
            ``"no-gateway"`` (kitty ran and resolved none), ``"not-installed"``
            (``kitty-bridge`` is not on disk, so it resolved nothing because it
            never ran), and empty (the gate itself never ran, because
            ``Configure kitty`` refused -- a setting missing, malformed, OR an
            egress shape it rejects with every setting parsing fine).

            ⚠️ **The CAUSE, not the gate's boolean.** ``proxied`` is ``"false"``
            for the first two alike, so passing it made this renderer report a
            dead pip mirror as an egress misconfiguration -- the same
            wrong-diagnosis defect, one surface over, on the surface added to
            fix it.
        configure_reason: ``Configure kitty``'s own ``reason`` output, which names
            the setting and field at fault. Rendered whenever the gate did not
            run, so this function stops INFERRING a cause it cannot see.

            🔴 **Three review rounds went to enumerating causes here, and each
            enumeration missed one.** The last was Configure's new egress SHAPE
            refusal: it parses every setting fine and still refuses -- an empty
            ``proxy_url``, an ``egress: null`` from *Remove gateway* -- while
            this renderer said *"no key or model set"* about settings that are
            present and healthy. Configure already names the field in its own
            output; reading it beats guessing from a tri-state, and it cannot
            drift as new refusal shapes are added.

    Returns:
        The Markdown body to write to ``$GITHUB_STEP_SUMMARY``.
    """

    lines = _headline(result, provider)

    if tiers:
        lines += ["", "| Tier | Outcome | Detail |", "| --- | --- | --- |"]
        for tier in tiers:
            if tier.available == "false":
                # 🔴 upstream. `available == "false"` now covers TWO causes, and
                # naming the wrong one here is the defect class this workflow
                # keeps having to remove -- a confident diagnosis pointing at
                # healthy settings. Rendering the old "no key or model set" on an
                # unproxied refusal sends an operator to check
                # KITTY_CREDENTIALS_JSON and KITTY_PROFILES_JSON, find both
                # perfectly fine, and reach the real cause only by opening the raw
                # job log. The gate's own verdict is what separates them: it is
                # `"false"` only when kitty ran and resolved no gateway, and empty
                # when the settings never parsed so the gate never ran.
                outcome = "not configured"
                if egress_cause == "no-gateway":
                    detail = "kitty resolved no egress gateway; skipped"
                elif egress_cause == "not-installed":
                    # Deliberately does NOT mention egress: kitty resolved
                    # nothing because it is not on disk, and the workflow's
                    # install branch exists precisely so this is not read as a
                    # misconfigured gateway (FR-6).
                    detail = "kitty-bridge is not installed; skipped"
                elif configure_reason:
                    # Configure refused, and it already said why -- by setting
                    # and field. Echoing it is what makes this table agree with
                    # the `::error::` annotation instead of contradicting it.
                    detail = f"{configure_reason}; skipped"
                else:
                    # Nothing told us why, so say exactly that much. The wording
                    # here was "no key or model set", which named a cause on
                    # evidence that cannot distinguish one.
                    detail = "a KITTY_* setting is missing or malformed; skipped"
            elif not tier.attempted:
                outcome, detail = "not run", "its Configure step did not execute"
            elif tier.status == "ok":
                outcome, detail = "produced the review", tier.reason
            else:
                outcome, detail = tier.status or "did not run", tier.reason
            lines.append(f"| {_cell(tier.name)} | {_cell(outcome)} | {_cell(detail)} |")

    # Only worth explaining when the provider actually ran and failed. One
    # skipped for a missing key, or never reached, does not invoke the action and
    # so leaves no annotations to explain.
    if any(t.attempted and t.status and t.status != "ok" for t in tiers):
        lines += ["", ANNOTATION_NOTE]

    return "\n".join(lines) + "\n"


def main() -> int:
    """Render the summary to a file.

    Returns:
        Always 0. A summary that cannot be written must not fail a run that
        otherwise succeeded.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--provider", default="")
    parser.add_argument(
        "--tier",
        action="append",
        default=[],
        metavar="NAME|AVAILABLE|STATUS|REASON",
        help="Repeatable, once per provider attempt.",
    )
    parser.add_argument(
        "--egress-cause",
        default="",
        help=(
            "upstream. Why the egress gate refused, verbatim from "
            "steps.egress.outputs.cause: 'no-gateway', 'not-installed', or empty when "
            "the gate never ran. Separates the three causes that share one "
            "`available=false`; the gate's own boolean cannot, because it is false for "
            "both 'no-gateway' and 'not-installed'."
        ),
    )
    parser.add_argument(
        "--configure-reason",
        default="",
        help=(
            "upstream. `Configure kitty`'s own `reason` output, naming the setting and "
            "field at fault. Rendered when the egress gate did not run, so the summary "
            "reports the cause Configure named instead of inferring one."
        ),
    )
    parser.add_argument("--out", required=True, help="Normally $GITHUB_STEP_SUMMARY.")
    parser.add_argument(
        "--ledger",
        default="",
        help=(
            "Path to the prompt redaction ledger (upstream). Appended to the summary so a "
            "review that ran on a redacted prompt says so where an operator will see it."
        ),
    )
    args = parser.parse_args()

    body = build(
        args.result,
        args.provider,
        [parse_tier(spec) for spec in args.tier],
        args.egress_cause,
        args.configure_reason,
    )

    # 🔴 Appended, and only when it says something happened. The ledger always exists -- an
    # artifact that appears only on the bad path reads as an alarm rather than as a record --
    # but repeating "nothing was moved" in the job summary on every run is noise that trains
    # people to skip the section on the run where it matters.
    if args.ledger:
        try:
            ledger = Path(args.ledger).read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"::warning::could not read the redaction ledger: {exc}",
                file=sys.stderr,
            )
        else:
            if "Nothing was moved" not in ledger:
                body += "\n\n---\n\n" + ledger

    try:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        print(f"::warning::could not write the run summary: {exc}", file=sys.stderr)
        return 0

    print(f"run summary written: result={args.result}, tiers={len(args.tier)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
