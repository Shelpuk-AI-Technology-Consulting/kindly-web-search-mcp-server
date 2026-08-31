# Automatic pull request review with GitHub Actions and Claude Code

## As Is

- The repository has **no `.github/` directory at all**: no workflows, no CI, no
  issue or PR templates. `claude-code-review.yml` is the first workflow it has
  ever had.
- Nothing runs `pytest` or `ruff` on a pull request. The 27 test modules under
  `tests/` are run by hand, if at all.
- Every pull request is reviewed by a human or not at all.
- There is no `.system_design/` directory and no `.requirements/` directory.
  `README.md` (~38 KB) is the de-facto specification: the tool contract, the
  client-by-client setup for seven MCP clients, the transport and allowlist
  behaviour, and the troubleshooting.
- `SECURITY.md` is the unmodified GitHub template — placeholder prose and a
  version table naming releases (`5.1.x`, `4.0.x`) this project has never had.
- The only repository secret is `SERPER_API_KEY`.

## To Be

Every pull request against `main` is reviewed automatically by Claude Code,
against a written contract plus the rules for the components it touches, with
findings posted as inline comments on the lines they are about. **A pull request
that was not reviewed fails a check** — a green tick has to mean a review
actually happened.

The system is adopted from an internal repository where it is in production, and
is kept structurally close to it so fixes carry across in both directions.

## Requirements

**R1 — Review every pull request against `main`.** On `opened`, `synchronize`,
`ready_for_review` and `reopened`. Same-repository, non-draft only.

**R2 — Judge the change against a specification, not against generic best
practice.** The reviewer reads the pull request conversation, then `README.md` in
full, then the diff — in that order.

**R3 — Load the rules for the components the change touches**, including a
fan-out so that a change to a module every other module imports pulls in its
consumers' rules.

**R4 — Post findings as inline comments**, severity-ordered, with a summary, and
degrade to a summary-plus-issue-comment when a finding cannot anchor to the diff.

**R5 — Reach the model ONLY through the configured Kitty Bridge egress
gateway.** A run that could reach a provider directly must not produce a review.

**R6 — Fail the check when no review was produced**, and say on the pull request
which of the two failure classes it was: `exhausted` (the provider could not
serve it — top up or wait) or `fatal` (the workflow or its settings).

**R7 — Keep the runner as small as the work allows.**

🔴 **Amended mid-implementation, and the original is kept because the reason is
the finding.** This was first written as *"on the shared self-hosted fleet"*,
matching the repository this system was adopted from. That turned out to be
unachievable rather than merely undesirable: a self-hosted runner group carries
an **"Allow public repositories"** setting that is off by default, and this
repository is public, so it reaches no group at all. Granting one would open a
shared fleet to anyone who can open a pull request — a decision with a real blast
radius, and not one this task should make silently. The requirement is therefore
satisfied on a GitHub-hosted runner instead.

**R8 — Leak nothing about the private repository this was adopted from.** This
repository is public.

**R9 — Test the review system itself.** It cannot test itself from inside the
review workflow: a broken rule selector still produces a green review, just a
shallower one.

**R10 — Block the merge when a review finding was closed with no readable
answer** — a resolved thread whose every comment has one author.

## Acceptance Criteria

**R1** — `on.pull_request` names all four types and `branches: [main]`. The job's
`if:` requires `draft == false` and `head.repo.full_name == github.repository`.
Concurrency cancels an in-flight review when a new commit lands.

**R2** — `REVIEW_PROMPT.md` names `README.md`, and names it before it mentions
the changed files. `ALWAYS_READ` in the test suite is the assertion. The
conversation is fetched by the workflow, not left to the agent, and is fenced as
untrusted: an attempt to direct the review through a comment is itself reported
as a critical finding.

**R3** — `select_rules.select()` returns the expected rule set for a path in each
component; `core` fans out to all four consumers; `tests/` does not fan out;
every rule name has a file; every rule file is reachable; every spec matches at
least one tracked path; every wildcard-free pattern names a file that exists or
is listed as forward-looking with a reason.

**R4** — `post_review.py` builds one review carrying the summary and every inline
comment; the posting step falls back to per-comment attachment when GitHub
rejects the batch, and lists what could not anchor in a marked issue comment.

**R5** — all five enforcement points present and asserted:
1. `KITTY_EGRESS_PROXY: ""` bound at workflow level;
2. `configure_kitty.py` refuses an egress document that parses but disables the
   gateway (missing envelope, `egress: null`, empty `proxy_url`);
3. a `kitty egress show` gate that exits 0 only when a gateway resolved, with
   both streams discarded so the proxy address is never echoed;
4. every model step — **both attempts** — gated on `proxied == 'true'`;
5. `Resolve outcome` ANDs the egress verdict, so an unproxied run resolves
   `fatal`.

Neither (2) nor (3) may be deleted as redundant: an empty `proxy_url` loads, so
(3) reports healthy while the connection goes direct.

**R6** — `Fail when no review was produced` exits 1 for any outcome other than
`ok`, with a distinct `::error::` per class; a notice is posted to the pull
request and superseded on a later successful run. No rendered surface asserts
that a re-run cannot help.

**R7** — `runs-on: ubuntu-slim` on all three jobs, named as a **literal** and
drawn from a written-down set, with the reasoning recorded at the declaration.
`test_the_review_job_names_a_known_runner_as_a_literal` enforces both halves: no
expression-valued runner, and no label outside the known set.

⚠️ **What that test cannot check, and neither can any offline guard:** whether
the label is actually offered to this repository. That is org state. It is called
out because the failure is silent — see the note below.

🔴 **An unreachable runner label is completely silent, and this cost most of a
day.** A job asking for a label nothing offers queues **for ever**: no error, no
annotation, no timeout. Measured here — three jobs queued over fifteen minutes
while a GitHub-hosted job on the same pull request finished in 55 seconds, with
the fleet demonstrably busy serving another repository throughout. Proved rather
than inferred: a throwaway workflow asking for the bare `self-hosted` label,
which matches every runner in every group a repository can see, was never claimed
either. A typo and an ungranted runner are indistinguishable.

**R8** — no file under `.github/` contains the private repository's name, a
sibling repository, a private project's issue-key namespace, or a fleet
hostname.

**R9** — `.github/workflows/ci.yml` runs `test_review_scripts.py` on every pull
request, on a bare interpreter with no dependency install.

**R10** — a `review_replies` job runs `check_review_replies.py` on every pull
request, with `pull-requests: read` declared rather than inherited.

## Testing Plan

`.github/review/tests/test_review_scripts.py` — 482 tests, `unittest`, runnable
on a bare interpreter with no installed dependencies (a broken review workflow
must be diagnosable without provisioning anything first).

| Layer | What it covers |
|---|---|
| Selector | the repository map, fan-out, prefix anchoring, literal-pattern existence, rule-file reachability, reachability from the real tree |
| Classifier | the `ok` / `exhausted` / `fatal` split, the retry verdict, timeouts, bridge stderr, schema-validation evidence |
| Rendering | failure notices, the superseded notice, the run summary, severity floors, truncation caps |
| Prompt | the always-read set, the eight dimensions, suppressive phrasings absent, root-object instruction |
| Workflow wiring | both attempts configured identically, one turn budget, no model pin, the egress gate, permissions, prior-review fetch ordering |
| Leak guard | no private-repository reference anywhere under `.github/`, with both a positive and a negative control |

**Known environment gap:** two `RetryGateWiringTests` cases fail on Windows and
pass in CI. Verified as environmental by running the **unmodified** reference
suite against the **unmodified** reference workflow on the same machine: the same
two cases fail there, so the failure is the local `bash`, not the port.

## Implementation Plan

1. Copy the 10 repository-agnostic scripts, the findings schema and the test
   suite **verbatim** → verify: they import and the suite runs.
2. Port `ci_preflight.sh`, rewriting only its header where it cited guards this
   repository does not have → verify: `--self-test` passes (18 cases).
3. Rewrite `select_rules.RULE_SPECS` for this repository's nine components →
   verify: smoke-test the selection for one path per component.
4. Write the nine rule files from the actual source, not from the reference's →
   verify: every rule file reachable, every rule name has a file.
5. Adapt `REVIEW_GUIDE.md` and `REVIEW_PROMPT.md` to `README.md` as the
   specification → verify: the prompt names it, before the diff.
6. Port the workflow; change the runner tier, the preflight path, and every
   sentence that cited an absent guard or document → verify: YAML parses, the
   wiring tests pass.
7. Add `ci.yml` with `review-scripts` and `review_replies` → verify: the wiring
   tests that read `ci.yml` pass.
8. Sanitise every private-repository reference and add the leak guard → verify:
   the guard trips on an injected leak and passes on the tree.
9. Add the review artifacts to `.gitignore` → verify: the artifact test passes.

## Implementation notes

- **`SECURITY.md` is left alone.** It is the GitHub template and replacing it is
  a separate decision. `docs.md` records that it is not a threat model so the
  reviewer does not judge changes against a fiction, and says not to re-raise it
  on every pull request.
- **No `pytest` / `ruff` job was added.** `ci.yml` says so in its own header
  rather than leaving it to be discovered. Adding one has its own decisions
  (which Python versions, what to do about the tests that need a browser, how to
  gate the live-network tests) and belongs in its own change.
- **The reply convention lives in `.github/review/README.md`, not `CLAUDE.md`.**
  The reference asserts it against a root `CLAUDE.md`; this repository
  `.gitignore`s that path, so the file the gate depends on would be untracked and
  a contributor cloning the repository would never see it.
- **Every measurement in the workflow's comments is inherited**, and each is
  marked as such. Nothing has been re-derived against this repository's much
  smaller pull requests.
- **Operator step, not done here:** the three `KITTY_*` organisation settings
  must be granted to this repository. No runner grant is needed — the workflows
  use a GitHub-hosted label precisely so that none is. `.github/review/README.md`
  § *Setting it up* is the checklist, and is the single place that list lives.

  ⚠️ The grant is easy to miss and, like the runner, fails quietly: an ungranted
  secret resolves to the **empty string** rather than erroring, which is why
  `configure_kitty.py` has to detect and report it itself. Verified on this
  repository — `GET /repos/{owner}/{repo}/actions/organization-secrets` returned
  0 while the same call against a repository that had been granted them returned
  all three. That endpoint is the fastest way to check the grant landed.
