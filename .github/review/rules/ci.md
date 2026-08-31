# Rule: CI and the review system (`.github/**`)

This directory contains one workflow — `claude-code-review.yml` — and the review
system it drives. **The reviewer is reviewing itself here**, so the bar is
higher, not lower: a defect in this tree degrades or disables review across the
repository without anything going red.

This review system was adopted from an internal repository where it is in
production. The structure is kept deliberately close to that one's so fixes can
be carried across; what differs here is the repository map, the rule files, and
the runner tier. **A change
that diverges from upstream for no stated reason costs that portability** — say
so when you see one.

## The egress guarantee — the invariant to defend hardest

**The reviewer must reach the model only through the configured Kitty Bridge
egress gateway.** Not "usually", not "when configured": a run that could reach a
provider directly must not produce a review. Five separate things enforce this,
and each is load-bearing. Treat the weakening of **any** of them as a
**critical** finding, and quote the line.

1. **`KITTY_EGRESS_PROXY: ""` at workflow level.** Kitty resolves its gateway in
   the order `--egress-proxy` flag, then this variable, then `egress.json`. So a
   value already in a self-hosted runner's environment under this name outranks
   the `KITTY_EGRESS_JSON` secret entirely. Binding it **empty** neutralises that
   — kitty reads it as `.strip()` and falls through to the file when falsy.
   Deleting the binding, or giving it a value, re-opens the hole.
2. **`configure_kitty.py` refuses a disabling egress shape.** A document without
   kitty's `{"version": …, "egress": …}` envelope, `{"version": 1, "egress":
   null}`, or a `proxy_url` of `""` all leave egress OFF while looking healthy.
   It reports `available=false` instead.
3. **The `Verify kitty resolved the egress gateway` step** asks kitty itself,
   with kitty's own resolver, on this machine, via `kitty egress show` — which
   exits 0 only when a gateway resolved. Both its streams are discarded on
   purpose: kitty's messages and table carry the proxy address, username and
   credential reference, and GitHub masks a secret's whole value rather than the
   JSON fields inside it, so echoing either stream publishes the gateway in the
   clear. **A change that logs those streams is a critical finding.**
4. **Every model step is gated on `steps.egress.outputs.proxied == 'true'`** —
   both attempts. A retry that omits the gate is the one launch in the job nobody
   watches, and it would run outside the bridge.
5. **`Resolve outcome` ANDs the egress verdict into `AVAILABLE`**, so a run
   refused for being unproxied resolves `fatal` and fails the check. A green check
   over an unproxied run is the outcome this whole structure exists to prevent.

Neither the static check (2) nor the live gate (3) subsumes the other: a
`proxy_url` of `""` **loads**, so kitty reports healthy and exits 0 at (3), while
aiohttp ignores `proxy=""` and connects directly. Do not let a change delete one
as "redundant".

## Kitty Bridge is the single writer of the CLI's environment

The workflow must **never** bind a name the Claude CLI reads — `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, or the
`ANTHROPIC_DEFAULT_*_MODEL` tiers — into an `env:` key. A value bound there
reaches the CLI **without** passing through the bridge, which is the only thing
that overrides the child's endpoint and credentials.

- The `anthropic_api_key:` action input is different and is deliberate: it only
  satisfies the action's non-empty startup gate, and kitty overrides the value in
  the child before it reaches any provider. It is bound to
  `secrets.KITTY_CREDENTIALS_JSON` so it rides on a secret that exists rather
  than on a retired one; a reference to a deleted secret resolves to `""` and the
  action never launches, reported as `fatal, no execution record` while pointing
  at settings that are all correct.
- **There is no model pin, and that is the design.** The model is the kitty
  *profile's*, injected into `~/.claude/settings.json`, whose env block outranks
  process env. A `--model` flag is a CLI argument and outranks both, so adding
  one does not pin the reviewer — it overrides the routing. A profile is a
  balancing pool of members with different models, so "the profile's model" names
  nothing to compare a pin against. **Flag any pull request that adds `--model`.**
- `path_to_claude_code_executable` pointing at the kitty wrapper is what makes
  the action use the bridge and skip its own CLI install. Omitting it on either
  attempt reverts that attempt to an unbridged CLI.

## Secrets in workflow YAML

- **Read secrets through `env:`, never by interpolating `${{ }}` into a `run:`
  script.** A `${{ }}` expression is substituted as *text* before bash sees the
  line, so a value containing a quote or `$(...)` is executed rather than
  compared. Flag the shape wherever it appears, even for a value only an admin
  can set — the next person copies it.
- Nothing that carries a secret may be uploaded as an artifact. The kitty debug
  log holds the bridge token and the full review prompt; only the **filtered
  timeline** travels, and the raw file stays on the runner. A change that adds it
  to `Upload review artifacts` is a critical finding.

## Prompt assembly

- The prompt is passed to the action as a single value, and past a size the OS
  refuses to start the process at all (`Argument list too long`). Measured
  upstream: 113,956 bytes served, 123,799 failed. `redact_prompt.py` bounds it and
  **never fails the step** — a shallower review beats a blocked merge.
- The `$GITHUB_OUTPUT` heredoc delimiter is randomised per run because the prompt
  carries the pull request conversation: a fixed delimiter is something a
  commenter could write on a line of its own to truncate the prompt or fail a
  required check. Do not replace it with a constant.
- `redact_prompt.py` splits on `<!-- REVIEW-SECTION: n -->` **markers, not
  headings**, for the same reason: a redactor that split on headings would take
  its structure from the untrusted input it exists to bound.

## Failure handling

- **Any outcome other than a posted review fails the job.** With one provider
  there is no next key, so "no review was produced" means the change would merge
  unreviewed and a green check would claim otherwise.
- The `exhausted` / `fatal` split is kept even though both fail, because it is the
  difference between "top up the balance" and "fix the workflow".
- **No rendered surface may assert that a re-run cannot help.** That claim was
  wrong twice: a provider that hangs produces the same empty execution record as
  a misconfiguration, and both observed occurrences cleared on a plain re-run.
- `continue-on-error: true` on the model steps, the notice builders and the
  evidence captures is deliberate: without it the job dies before anything
  classifies the failure, comments on the pull request or writes the summary — a
  red check with no explanation anywhere. Removing one is a finding; adding one
  to a step whose failure *should* fail the run is also a finding.
- A step that runs between `Interpret` and `Resolve` and can fail without
  `continue-on-error` will suppress a review that had already succeeded, because
  the later steps carry an implicit `success()`.

## Runner and caps

- `runs-on: [self-hosted, cap-light, noble]`. The labels are cumulative and
  declare **memory**: `cap-pico` ≥ 900 MB, `cap-nano` ≥ 1.9 GB, `cap-light` ≥ 3.9 GB,
  `cap-main` ≥ 7.7 GB. This is the tier upstream uses, because the Bun and CLI
  install wants the memory. **If a change moves it, the direction and the reason
  both need stating** — and an OOM during the install is what a too-small tier
  looks like, not a slow review, so a move DOWN needs a measured peak RSS rather
  than an argument.
- **A label is not access.** A runner group is granted per repository, and a job
  asking for a label no granted machine carries queues **for ever** — no error, no
  annotation, no timeout. A misspelled capability does exactly the same thing. So
  an indefinitely queued job is never evidence about the tier on its own; check
  the grant first.
- A capability label declares memory and **nothing else**, which is why
  `ci_preflight.sh` probes for `unzip` before the action shells out to it. The
  fleet is not homogeneous. A new job that invokes a tool without a preflight call
  fails only when placement is unlucky, and the re-run passes — the most expensive
  failure class there is.
- `timeout-minutes` is a backstop, sized for two attempts' tails. `API_TIMEOUT_MS`
  is what bounds a single hung call and must stay well below it, so a hang fails
  as a timeout with a diagnostic rather than being killed by the job cap with
  none.

## Forks

The job runs only for same-repository, non-draft pull requests. **This repository
is public and the runners are self-hosted**, so that condition is a security
control, not a convenience: a fork PR must never place a job on this fleet.
Weakening the `if:` — adding `pull_request_target`, or dropping the head-repo
comparison — is a **critical** finding.

## Actions and pinning

- Third-party actions are referenced at a floating major (`@v1`, `@v6`). The
  Claude CLI version installed alongside is pinned as a literal and is re-synced
  as one change when the action bumps. A CLI version bump with no note about the
  action's own pin breaks that pairing.
- `permissions:` is least-privilege and each grant has a reason:
  `issues: write` is there because conversation-tab comments are *issue* comments
  and two steps POST them — a read grant 403s on exactly the run that has nothing
  else left to tell anybody.

## The scripts

Every script under `.github/review/scripts/` except `select_rules.py` is carried
from upstream **verbatim** so fixes stay portable. A change to one of them here
is a fork: it needs a stated reason and it should be considered for upstreaming.
`select_rules.py`'s `RULE_SPECS` is the half that is ours — a new source
directory that no pattern matches means pull requests touching it are reviewed
with **zero rule files loaded**, which is the defect shape upstream recorded when
a 397-file directory matched nothing for months.
