# Test suite implementation plan

Executable plan for `.system_design/TEST_SUITE.md`. Every step traces to a section
of that document; this file adds sequencing, dependencies and acceptance criteria,
not new design.

---

## 1. Rules

### 1.1 CI arrives with green, not before it

The repository has 11 known failures (TEST_SUITE §1.1), so there is no honest way
to run a required suite until E1-6 restores it. Attempting to enforce "no new
failures" in the meantime does not work either: a per-OS **count** cannot tell the
difference between fixing one stale test and breaking a different one.

So there is no interim enforcement phase. **E4-1 is authored early and merges with
`complete E1-6`**, landing green, and E4-2 makes it required immediately after —
which is what TEST_SUITE §8B asks for. Every later job is added and activated
incrementally on top of an already-enforced gate.

Before E1-6 the repository has no CI, exactly as today. That is the honest
position, and it is short.

### 1.2 A job never becomes required before its tests exist

That is the actual invariant. A marker-selected job enabled before its tests exist
is red by construction under `--min-selected`, and the tests that would fix it
then cannot merge past the job they need to fix.

It does **not** follow that every job needs a separate activation step. Where the
job's tests have already merged and its runtime is ordinary, creating it and
adding it to `ci-required.needs` in one PR is safe and is what these steps do — a
job that cannot pass simply does not merge. The add/activate split is reserved for
jobs whose **CI runtime is unproven**, currently only `chromium`, where the
container may behave differently than it does locally and landing it in
reporting-only mode first is what allows that to be debugged without blocking the
branch.

### 1.3 Workflow files are a contended resource

Steps that add jobs edit the same workflow, and the graph says several can proceed
in parallel. To keep that true mechanically, **each job is defined in its own
reusable workflow file** under `.github/workflows/`, and `ci.yml` does nothing but
call them and declare `ci-required.needs`. The `needs` lists are then the only
contended regions, and the steps mutating each are **totally ordered** — declared
here and checked by `scripts/check_plan_dag.py`, which fails if any listed step
does not depend on the one before it. Prose promising serialization is not
serialization; two PRs editing one `needs` list in parallel conflict, and the
worse outcome is a rebase that silently drops the other's edit.

| Region | Mutated by, in order |
|---|---|
| `ci.yml.jobs` | E4-1, E4-3, E4-4, E4-5, E4-11, E10-3, E4-9 |
| `ci-required.needs` | E4-1, E4-3, E4-4, E4-5, E4-11, E4-10, E10-10 |
| `nightly.yml.jobs` | E4-12, E12-1, E4-13 |
| `nightly-summary.needs` | E4-12, E12-1, E4-13 |

**The caller job is contended too, not just `needs`.** A reusable workflow reports
nothing until `ci.yml` calls it, so a "reporting-only" step still edits `ci.yml`.
Both regions are tracked; a step's reusable-workflow *file* remains uncontended and
can be written in parallel with anything.

**The chain order is chosen to keep external prerequisites off it where it can
be.** `package` and the reporting-only `coverage` job are wired before `chromium`,
so the container registry (X-3) does not gate them; and `mutation` attaches to the
nightly before the live jobs, so the live-query budget owner (X-4) does not gate
it. An ordering that put `chromium` first would have made the registry a
prerequisite of nearly every later job.

Chromium cannot be pushed further back than this: E10-5 measures the `chromium`
job's own coverage and E10-6 asserts on it, so the coverage *controls* — though not
the coverage job — genuinely depend on X-3. Ordering `chromium` after them
produced a dependency cycle, which is how that constraint was found.

### 1.4 Red and green ship together

A test-only PR that fails cannot merge under the gate. A new assertion and the
code satisfying it are one PR. Where a test must be seen failing first — and it
must — that happens in the author's tree, evidenced in the PR description.

### 1.5 Dependency kinds

Validated against the target's declared Type by `scripts/check_plan_dag.py`,
which also uses them to compute what is authorable: only an unsatisfied `impl`
dependency stops a step being written.

| Kind | Meaning | Valid targets |
|---|---|---|
| `impl` | The artefact or decision is needed **before authoring** | any |
| `merge` | Authoring proceeds now; a prerequisite **PR** must land first | `PR` |
| `complete` | Authoring proceeds now; a **non-PR** prerequisite must finish before this step merges or activates | milestone, decision, operation, external |

A step blocked only by `merge` or `complete` can be written today. A step with any
`impl` dependency cannot.

### 1.6 Step types

`PR` (a reviewable change), `milestone` (a state assertion, no diff),
`decision` (a recorded choice), `operation` (a repository or infrastructure
action).

### 1.7 Conventions

- One PR per `PR` step. If it needs two, it is two steps.
- Useful and correct on merge; no later step required to make it so.
- An acceptance check a reviewer can run, naming a fault to inject where the step
  adds tests.
- Sizes: S under half a day, M half to two days, L two to five days.
- **A review point we decide not to act on is recorded here, not only in the PR
  thread.** It gets a `> **Deferred:**` block at the place it applies, naming what
  was raised and why it was not taken. A decision that lives only in a review
  conversation is invisible to the next reader, who then raises it again — which is
  how several items in this document were rediscovered three or four times.

---

## 2. Sequencing for parallelism

<!-- totals: steps=78 authorable=51 pr_authorable=47 -->

Only **E0-1 and E0-2** are serial — a dependency declaration and a pytest config
block, both S. Everything else fans out. The validator computes what is
*authorable* using §1.5's semantics — only an unsatisfied `impl` dependency stops
a step being written.

The figure that matters for staffing is **`pr_authorable` in the declaration
above: the number of `PR` steps an engineer can pick up the moment E0-2 lands.**
The larger `authorable` count includes milestones and an admin operation, which
are not work anyone can start, and quoting it overstates capacity.

A step waiting only on `merge` or `complete` is written now and simply queued to
land, which is the distinction that makes this plan distributable.

Both figures are checked by `check_declared_totals`. **Numbers appear once, in the
declaration above.** A figure repeated in prose drifts the moment the table
changes — and the validator checks only the declaration — so the rest of this
document refers to it rather than restating it.

Each stream starts on its own narrow dependency, not on a wave.

| # | Stream | Starts after | Steps |
|---|---|---|---|
| 1 | Worker seam & protocol | E0-2 | E2-1, E2-2, E2-3, E2-4 |
| 2 | Baseline restoration | E0-2 | E1-1, E1-2, E1-3, E1-5 |
| 3 | Fixtures & corpus | E0-2 | E3-1, E3-2, E3-3, E3-4, E3-5 |
| 4 | CI skeleton | E0-3 | E4-1, E4-12 |
| 5 | L1 parsers | E0-1 | E5-1, E5-2 |
| 6 | L1 resolvers & accumulators | E0-2 | E5-3, E5-6, E5-7 |
| 7 | L2 contracts | E0-2 | E6-1, E6-3 |
| 8 | L4 live canary | E0-2 | E8-4 |
| 9 | Coverage configs | E0-1 | E0-4 |

Opening later: L3 server (E3-4), L3 worker (E2-3 + E3-1 + E6-2), L4 product
(E3-2 + E3-5), coverage controls (E2-3 → E10-1), ChromiumPool (E3-4), security
sanitizer (E5-7), transforms (E3-3).

---

## 3. External prerequisites

Not reviewable PRs. Each needs an owner, evidence of completion, and a **rollback
note recorded before any dependent step activates**.

| ID | Prerequisite | Blocks | Owner |
|---|---|---|---|
| **X-2** | Repository admin authority — branch protection, require a PR, no bypass actors | E4-2 | repo admin |
| **X-3** | Container registry — registry choice, publish credentials as Actions secrets, and a decision on whether fork PRs may pull the image | E4-7 | repo admin |
| **X-4** | Live-query budget owner — billing authorisation for nightly spend and a named person alerted on nightly failure. `SERPER_API_KEY` already exists. | E4-13 | maintainer |
| **X-5** | Observational coverage reviewer and cadence (TEST_SUITE §10.4 requires a named owner; unowned reporting decays into the signal nobody reads) | E10-7 | maintainer |
| **X-6** | Owners for every row of TEST_SUITE §9's risk-to-test matrix. X-4 and X-5 name two; the column is otherwise blank, and TEST_SUITE §13.3 leaves it open | E13-2 | maintainer |

**X-1 is answered** (TEST_SUITE §13.1, decided 2026-08-31), so nothing in this
plan is blocked from being *written* any more. E9-0 — the step that existed to
materialise the chosen branch — is complete, and the E9 steps are now concrete
rather than conditional.

The remaining prerequisites block merge or activation only, so the work they gate
can be written first.

**E13-2** writes X-6's answer into `TEST_SUITE.md` §9's Owner column. Without it
X-6 could be "complete" with names in a ticket while the design that is supposed
to be authoritative still shows a blank column, and E13-1 would pass.

**X-3 does not gate writing ChromiumPool tests.** E4-6 builds and smoke-tests the
image locally with no registry, and E7-3 merges against that; only the CI job
waits on publication.

**`outputSchema is None` is normative for this implementation** (E6-1), per
TEST_SUITE §13.2. Changing it is a new step, not a pending decision — which is why
it carries no X-number.

---

## 4. Epics

### E0 — Bootstrap

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E0-1** | Apply §10.2's dependency constraints in full | PR | — | S |
| **E0-2** | `[tool.pytest.ini_options]` with markers | PR | impl E0-1 | S |
| **E0-3** | `--min-selected` collection guard | PR | impl E0-2 | S |
| **E0-4** | The three coverage configs | PR | impl E0-1 | S |

- **E0-1.** The **whole** table in §10.2, not just the new entries: add
  `pytest-asyncio`, `hypothesis`, `coverage`, `diff-cover`, `mypy`, **and bound
  the existing `pytest`, `ruff` and `packaging`, which are currently unbounded**.
  `mutmut>=3,<4` goes in a separate `mutation` extra so the nightly can install it
  without putting a Linux-only tool in `dev`. Add a `ratchet` extra and generate
  `requirements-ratchet.txt` per §10.4.
  *Verify:* `pip install -e .[dev]` succeeds on both platforms;
  `pip install -e .[mutation]` provides a working `mutmut`; `test_dependency_constraints.py`
  is extended to assert every §10.2 bound and fails if one is removed; the
  lockfile has no local path or VCS URL.
- **E0-2.** *Verify:* `pytest --markers` lists all seven; an unregistered marker
  fails collection; pass/fail counts unchanged.
- **E0-3.** *Verify:* `-m <unmatched> --min-selected=1` exits 4 with the count in
  the message; a real failure with a satisfied minimum exits 1; a satisfied
  minimum passes. `pytest_collection_modifyitems` is the wrong hook — it runs
  before mark deselection and never fires.
- **E0-4.** *Verify:* with `.coveragerc`, `coverage json` includes
  `scrape/chromium_pool.py` at zero — the observable proving `source_pkgs` works;
  with `.coveragerc-gate` it is absent.

---

### E1 — Restore green

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E1-1** | Record the per-OS baseline | PR | impl E0-2 | S |
| **E1-2** | Move sandbox flag/default assertions to L1 | PR | impl E0-2 | M |
| **E1-3** | Keep retry/cleanup orchestration at `_fetch_html` | PR | impl E1-2 | M |
| **E1-4** | Repair the three loader tests | PR | impl E2-1 | M |
| **E1-5** | Rewrite the concurrency test OS-neutral | PR | impl E0-2 | S |
| **E1-6** | Suite green on both platforms | milestone | merge E1-1, merge E1-3, merge E1-4, merge E1-5 | S |

- **E1-1.** §1.1 predicts 12 POSIX failures *from source, unmeasured*. Run it.
  *Verify:* both per-OS numbers, **and the exact failing node ids**, committed to
  `.system_design/BASELINE_FAILURES.md`. A count cannot tell a fixed test from a
  swapped one; the ledger is what makes the remaining repairs checkable.
  **E1-2 through E1-5 each delete exactly the node ids they repair from that
  ledger and add none**, so it drains to empty as the repairs land rather than
  sitting stale next to a green suite.
- **E1-2.** Direct tests of `_build_chromium_launch_args`,
  `_resolve_sandbox_enabled`, `_resolve_browser_executable_path`,
  `_resolve_start_retry_attempts` with §3.1's ambient-state seams, in
  `tests/test_nodriver_worker_launch_resolvers.py`. **E1-2 owns
  `_build_chromium_launch_args` in full**, including the branches that emit
  `--proxy-server` and `--proxy-bypass-list` and the append-last ordering of the
  caller's base arguments. `test_worker_launch_args_redaction.py` keeps its
  credential-handling assertions, one of which — Chromium receives the *real*
  `--proxy-server=` value while the emitted copy is redacted — necessarily also
  pins that the flag is emitted at all; that overlap is the point of the
  assertion and is left alone. What E1-2 owns is the *branch conditions*: when
  each proxy flag appears and when it does not. Stated because E5-4 is scoped by
  *exclusion* — "the
  remaining resolvers not covered by E1-2" — so a branch this step declines is
  orphaned permanently rather than deferred.
  *Verify:* each fails when its resolver's default is inverted; `--no-sandbox`,
  root detection and executable discovery all survive; no new test calls
  `_fetch_html`; the node ids it repairs are removed from the E1-1 ledger and no
  new id is added.
- **E1-3.** Sequenced after E1-2: both edit the same eight stale tests. E1-2
  removes the flag half; E1-3 rewrites what remains with autospec doubles around
  the browser-connect call. **These stay at this layer permanently** —
  `_fetch_html` is the child process's own function and owns browser startup,
  connect retry and profile cleanup; `_run_worker_command` is parent-side.
  **E1-3 also inherits one claim from E1-2's half.**
  `test_errors_when_no_browser_found` asserted two things: that
  `_resolve_browser_executable_path` finds nothing, and that `_fetch_html` turns
  that `None` into a `RuntimeError` naming `KINDLY_BROWSER_EXECUTABLE_PATH`.
  Only the first is a resolver claim. The second is the only actionable guidance
  a user gets when no browser is installed, it is raised inside `_fetch_html`,
  and E1-2 cannot take it without calling `_fetch_html` — which its own *Verify*
  forbids. It is E1-3's.
  *Verify:* retry-count, terminate-failed-attempt, non-retryable-not-retried and
  profile-cleanup pass; a missing browser executable surfaces the install and
  `KINDLY_BROWSER_EXECUTABLE_PATH` guidance rather than a bare `TypeError` or an
  error from Chromium; adding a required argument to the patched callable makes
  them fail rather than silently pass; the ledger shrinks by exactly the ids
  repaired.
- **E1-4.** Rebuild `_FakeProc` as E2-1's typed fake.
  *Verify:* all three pass; deleting `stdout` from the fake fails both mypy and
  the runtime conformance test; the ledger shrinks by exactly the ids repaired.
- **E1-5.** *Verify:* passes on both platforms; each retained case (explicit,
  malformed, zero, negative, `num_results` limit) fails if the clamp is removed;
  the ledger shrinks by exactly the id repaired.
- **E1-6.** No diff. *Verify:* **0 failed** on Windows and Linux, and
  `BASELINE_FAILURES.md` lists no remaining ids — the two facts are checked
  together, since either alone can be true while the other is not. CI enforcement
  begins here. **The Windows half must come from a real Windows run**, not from
  the document: once E1-2 drained the root-detection case the two platform
  blocks became identical, which makes the cross-platform difference check a
  tautology, and nothing else asserts the Windows block until E4-1's matrix
  exists. A drained document is evidence of nothing on a platform nobody ran.

---

### E2 — Worker seam and protocol

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E2-1** | `WorkerProcess` Protocol, typed fake, mypy harness | PR | impl E0-1 | M |
| **E2-2** | Extract `_build_worker_command` | PR | impl E0-2 | S |
| **E2-3** | Extract `_run_worker_command` into `scrape/worker_runner.py` | PR | impl E2-2, merge E1-4 | M |
| **E2-4** | Annotate `_run_worker_command` with `WorkerProcess` | PR | impl E2-1, impl E2-3 | S |

- **E2-1.** **The Protocol lives in production**, in a small type-only module —
  `src/kindly_web_search_mcp_server/scrape/types.py`. It has to: E2-4 annotates
  production code with it, and production importing from `tests/` is not an
  option. The step is a production change that adds no behaviour.
  The typed fake under `tests/` imports that canonical Protocol, so there is one
  definition rather than two that can drift.
  **The negative fixture is excluded from the ordinary mypy target.** A file that
  must fail type-checking cannot sit in the path the `types` job checks, or that
  job is red forever. It lives under `tests/typing_negative/`, is excluded in mypy
  config, and is exercised by a harness that shells out to mypy, asserts a
  non-zero exit, and asserts the *specific* diagnostic code.
  **Two consequences of §10.2's `mypy>=2.3.1,<3`.** mypy 2.x enables
  `--strict-bytes` by default, so the fake's `stdout`/`stderr` must return `bytes`
  — a buffer-backed fake returning `bytearray` or `memoryview` no longer
  type-checks. And this step must **state the harness's marker**: it shells out to
  mypy, which §10.5's `subsystem` definition ("a real socket or child process")
  covers, but §10.4 currently puts `mypy` in the `ratchet` extra on the assumption
  the harness stays in the hermetic set. Mark it `subsystem` and `mypy` leaves
  both the `ratchet` extra and the lockfile.
  *Verify:* the harness fails if the negative fixture starts type-checking
  cleanly; removing `stdout` from the fake fails mypy and the conformance test; a
  test documents that `isinstance` alone does not catch a wrong `wait()`
  signature; no module under `src/` imports from `tests/`.
- **E2-2.** **Production change.** *Verify:* an L1 test asserts the command shape
  including `-m kindly_web_search_mcp_server.scrape.nodriver_worker`; existing
  tests unchanged.
- **E2-3.** **Production change, highest-leverage step in the plan** — it opens the
  L3 worker stream and makes the coverage classification expressible (§10.4,
  §11.2). E1-4 lands first so a green characterization baseline exists.
  *Verify:* `fetch_html_via_nodriver` behaviour unchanged (E1-4's tests pass);
  `_run_worker_command` importable and accepts an arbitrary command; no `command=`
  parameter on any public function; `universal_html.py` retains the Markdown-probe
  path and no subprocess management.
- **E2-4.** **Production change**, annotation only, since E2-1 already placed the
  Protocol in `src/`. *Verify:* mypy checks the production signature against the
  Protocol; changing the Protocol without the function fails.

---

### E3 — Fixtures and corpus

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E3-1** | Fixture child process script | PR | impl E0-2 | M |
| **E3-2** | Local SearXNG-contract HTTP server fixture | PR | impl E0-2 | M |
| **E3-3** | HTML corpus scaffolding and governance | PR | impl E0-2 | M |
| **E3-4** | Anti-flake harness helpers | PR | impl E0-2 | M |
| **E3-5** | `tests/package/` and its marker policy test | PR | impl E0-2 | S |
| **E3-6** | Injectable DNS-resolution and transport seam | PR | impl E0-2 | M |

- **E3-1.** Emits known `KINDLY_DIAG` frames, can hang, can exit non-zero, can
  write garbage to stderr. *Verify:* a smoke test drives each mode. **Readiness is
  polled with a hard timeout of at least 30 s, never asserted against a fixed
  startup budget** — a millisecond threshold is a flake generator on loaded runners
  and under antivirus process-start delay. Startup duration is printed as
  telemetry, never a pass condition.
- **E3-2.** Ephemeral port, readiness handshake, configurable result set including
  **zero results** (§6.1). *Verify:* `search_searxng` parses its responses; with
  `SEARXNG_BASE_URL` pointed at it and higher-priority provider variables cleared,
  `search_web` selects SearXNG; zero-result mode returns `[]`.
- **E3-3.** `tests/corpus/html/`, the `.meta.json` sidecar schema, a policy test
  for §3.3. *Verify:* the policy test fails on a snapshot with no sidecar; over
  200 KB; a sidecar missing **source URL, capture date, or licence/rationale**; and
  content matching **sanitation patterns — `Set-Cookie`, `Authorization`,
  bearer-shaped tokens, email addresses, known analytics script bodies**. Seed with
  at least three handcrafted fragments.
- **E3-4.** §5.4: readiness handshake, ephemeral ports, isolated profile
  directories, PID-tree cleanup keyed on spawned PIDs, child log capture on
  failure. Test-only.
  *Verify:* a hanging fixture child is killed at the deadline and its PID tree is
  gone; cleanup never matches processes by name.
- **E3-5.** *Verify:* a file added there without `@pytest.mark.package` fails the
  policy test; source-checkout jobs pass `--ignore=tests/package`.
- **E3-6.** **Production change**, not a test helper. E9-5 must observe a hostname
  resolving differently at validation and at connect, and that cannot be faked
  from outside: the resolver and transport have to be injectable in the code that
  performs the fetch.

  **This step does not wait on X-1.** The seam's API is the same whichever way the
  policy goes — a resolver callable and a transport factory, both defaulting to
  today's behaviour. Only what *attaches* to it differs: under a permissive policy
  tests script it, under a restrictive one enforcement wraps it. Blocking it on
  E9-0 would have been false blocking on the one seam every retained rebinding
  test needs.
  *Verify:* the seam has an explicit API (a resolver callable and a transport
  factory, both defaulting to today's behaviour); production behaviour is
  unchanged with the defaults; a test can script successive lookups of one
  hostname to return different addresses.

---

### E4 — CI and enforcement

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E4-1** | Workflow skeleton, broad job, `ci-required` | PR | impl E0-3, complete E1-6 | M |
| **E4-2** | Enable branch protection | operation | complete X-2, merge E4-1 | S |
| **E4-3** | Split into the normative `fast` and `subsystem` jobs | PR | merge E4-1, complete E4-2 | M |
| **E4-4** | Add and activate the `fast-extras` job | PR | merge E4-3 | S |
| **E4-5** | Add and activate the `types` job | PR | merge E2-4, merge E4-4 | S |
| **E4-6** | Define and locally smoke-test the runtime image | PR | impl E0-1 | M |
| **E4-7** | Publish the image to the registry | PR | merge E4-6, complete X-3 | M |
| **E4-8** | Record and validate the image digest | PR | merge E4-7 | S |
| **E4-9** | Add the `chromium` job, reporting only | PR | merge E7-3, merge E4-8, merge E10-3 | S |
| **E4-10** | Activate the `chromium` job | PR | merge E4-9, merge E4-11 | S |
| **E4-11** | Add and activate the `package` job | PR | merge E8-1, merge E4-5 | S |
| **E4-12** | Nightly workflow foundation | PR | impl E0-3 | S |
| **E4-13** | Add the live jobs to the nightly | PR | merge E8-4, merge E8-5, merge E4-8, merge E12-1, complete X-4 | M |

**Every marker-selected job carries a `--min-selected` floor.** That is stated
once here rather than repeated per step, and it applies to the broad job,
`fast`, `subsystem`, `fast-extras`, `chromium`, `package`, both live jobs and the
hermetic coverage selection. **`types` is excluded**: it runs `mypy`, not a
marker-selected pytest invocation, so the option does not apply. Its equivalent
non-vacuity check is in E4-5 — the configured target modules must exist and be
reported as checked, so a job pointed at a path that no longer exists fails
instead of passing with nothing to do. A floor is committed alongside the workflow
and **updated in the same PR whenever tests are deliberately added to or removed
from that selection**; a job whose selector silently stops matching is otherwise
green while running nothing. Each job's acceptance includes: a deliberately
typo'd selector fails the job.

- **E4-1.** §10.3's complete trigger list; one job selecting
  `--ignore=tests/package -m "not live and not chromium and not package"` on both
  platforms — deliberately **including** `subsystem`, per TEST_SUITE §8B; and
  `ci-required` with `if: always()` asserting every dependency is `success`.
  Merges only once E1-6 lands, so it is green from its first run.
  *Verify:* a failing test makes `ci-required` red; a **skipped** dependency also
  makes it red; a fork PR triggers the workflow; the job is green on merge.
- **E4-2.** No diff to the repository. This is §8B's minimum viable gate and it
  lands immediately after green — which is why E4-3 declares `complete E4-2`
  rather than relying on prose ordering.
  *Verify:* a PR with a red `ci-required` cannot be merged, including by an
  administrator; the rollback procedure is recorded in the ticket.
- **E4-3.** Narrow the broad job to `-m "not live and not subsystem and not
  chromium and not package"` over Python 3.13/3.14 × mcp {min, max}, and add
  `subsystem` selecting `-m "subsystem and not chromium and not live"` on both
  platforms; both join `ci-required.needs`.
  *Verify:* every test that ran in E4-1's broad job runs in exactly one of the
  two; `--min-selected` floors are set from the counts observed in E4-1.
- **E4-4.** The `fast` selector with `pdf-advanced` installed, Python 3.13 only.
  Created and activated together: its tests already exist, so §1.2's split does
  not apply. *Verify:* a PDF-path test that skips without the extras runs here;
  `fast-extras` is in `ci-required.needs` and the aggregate is green.
- **E4-5.** Likewise created and activated together. `merge E2-4`, not `merge
  E2-1`: the job's whole purpose is catching signature drift on
  `_run_worker_command`, which does not exist until E2-3 and is not annotated
  until E2-4. Created earlier, its target list would omit the one signature it
  exists to check, and the non-vacuity rule below would then prevent adding a
  target that did not yet exist. *Verify:* the negative-fixture directory is excluded; **`types` is in
  `ci-required.needs`**; introducing an `Any` on the Protocol surface makes it red;
  and mypy's output names every configured target module, so a target that no
  longer exists fails the job rather than leaving it green with nothing checked.
- **E4-6.** The image definition — Python, Chromium, system deps, **no application
  code** — from a pinned base digest with pinned package versions, built and smoke
  tested **locally**. No registry, so it does not wait on X-3, and E7-3 can merge
  against it.
  *Verify:* `docker build` succeeds from the pinned base; a container from it
  launches Chromium and runs a trivial fetch.
- **E4-7.** Publish to the registry chosen in X-3, with credentials as Actions
  secrets. *Verify:* the published manifest matches the local build; the fork-pull
  decision from X-3 is recorded in the workflow.
- **E4-8.** Record the digest in the repository and add the check that CI
  references it by digest, never by tag. *Verify:* changing the tag without the
  digest does not change what CI pulls; a mismatched digest fails the job.
- **E4-9.** Adds the job, **not** to `ci-required`. This is the one place §1.2's
  split applies: the container's CI runtime is unproven and may differ from the
  local smoke test in E4-6. *Verify:* it runs E7-3's tests with a real
  `--min-selected` and is green; the wheel-resolution assertion holds.
- **E4-10.** One line. *Verify:* `chromium` is in `ci-required.needs` and the
  aggregate is green.
- **E4-11.** `tests/package -m package`, Python 3.13 × mcp {min, max}, created and
  activated together. `merge E4-5` sequences its `ci-required.needs` edit behind
  the previous one, per §1.3, and keeps it ahead of `chromium` so the registry
  does not gate it. *Verify:* green against E8-1's tests and present in
  the aggregator.
- **E4-12.** A `schedule`-triggered workflow with `workflow_dispatch` and a
  `nightly-summary` aggregator, and **no jobs yet**. Exists so mutation and live
  work attach independently. *Verify:* a manual dispatch runs and the summary
  reports zero jobs without failing.
- **E4-13.** `live-serper` and `live-extraction`, each with its **own** credential
  criteria — they are not the same case. `merge E4-8` because `live-extraction`
  needs a browser and therefore the runtime image.
  *Verify:* removing `SERPER_API_KEY` makes **`live-serper`** fail before
  collection, while **`live-extraction` still runs**, since it needs a browser and
  no provider credential; `KINDLY_RUN_LIVE_TESTS=1` is set in both job envs; the
  summary reports skip counts; fork PRs never receive the secret; both join
  `nightly-summary.needs`.

---

### E5 — L1 component and property tests

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E5-1** | Parser mutual-exclusivity property | PR | impl E0-1 | M |
| **E5-2** | Per-parser identifier preservation and rejection | PR | impl E0-1 | M |
| **E5-3** | Server, search and pool configuration resolvers | PR | impl E0-2 | M |
| **E5-4** | Nodriver launch and sandbox resolvers | PR | impl E1-2 | S |
| **E5-5** | Markdown transforms against the corpus | PR | impl E3-3 | M |
| **E5-6** | Text accumulators and encoding-cookie helpers | PR | impl E0-2 | S |
| **E5-7** | Redaction helper units — existing behaviour only | PR | impl E0-2 | M |

E5-3 and E5-4 own **disjoint** function sets, so they can run in parallel without
duplicating tests or touching the same files.

- **E5-1.** Hypothesis over generated URLs. *Verify:* fails if any parser's
  matching is widened to overlap another; the shrunk counterexample is readable.
- **E5-2.** For each of the five parsers: a generated URL built from known
  identifiers returns exactly those identifiers; a rejected URL raises that
  parser's own error type; rejection is stable across trailing slashes, scheme
  case and query order. *Verify:* each property fails if its parser's group
  extraction is off by one, and if the parser raises bare `Exception`.
- **E5-3.** Owns `server.py`'s `_resolve_transport`, `_resolve_host_port`,
  `_resolve_tool_total_timeout_seconds`, `_resolve_web_search_max_concurrency`,
  `_resolve_transport_security`, `_cors_origin_regex`, `_get_int_env`,
  `_get_float_env`; and `chromium_pool.py`'s `_resolve_reuse_enabled`,
  `_resolve_pool_size`, `_resolve_acquire_timeout_seconds`, `_parse_port_range`,
  `_resolve_port_range`, `_pick_port`.
  *Verify:* grouped by behaviour, because one criterion does not fit all of them.
  **Numeric env parsing** (`_get_int_env`, `_get_float_env`, `_resolve_pool_size`,
  `_resolve_acquire_timeout_seconds`, `_resolve_tool_total_timeout_seconds`,
  `_resolve_web_search_max_concurrency`, `_resolve_host_port`): unset, blank,
  malformed, zero, negative, out-of-range. **Enum and boolean parsing**
  (`_resolve_transport`, `_resolve_reuse_enabled`): unset, blank, each accepted
  value, an unrecognised value. **Range parsing** (`_parse_port_range`,
  `_resolve_port_range`): well-formed, inverted bounds, non-numeric, empty.
  **Port selection** (`_pick_port`): a port inside the configured range, and
  behaviour when every port in the range is occupied. **Pattern construction**
  (`_resolve_transport_security`, `_cors_origin_regex`): the produced allowlist
  and regex match the loopback set and reject a foreign origin. Every case fails
  if its branch is removed.
- **E5-4.** Owns the remaining `nodriver_worker.py` resolvers not covered by E1-2:
  `_resolve_user_agent`, `_detect_chrome_version`, `_resolve_retry_backoff_seconds`,
  `_resolve_devtools_ready_timeout_seconds`, `_resolve_worker_timeout_seconds`,
  `_resolve_worker_timeout_details`, `_resolve_snap_backoff_multiplier`,
  `_resolve_chrome_proxy`, `_resolve_chrome_proxy_bypass`, `_split_no_proxy_value`,
  plus `_is_snap_browser` and `_is_retryable_browser_connect_error`. Those last
  two were named as component-level targets in §3.1 but claimed by no step —
  found while implementing E1-2, which declined them rather than absorb work no
  one had scoped. Nothing checks §3.1's target list against step ownership, so
  the gap would not have surfaced twice.
  *Verify:* grouped by behaviour. **Numeric env parsing**
  (`_resolve_retry_backoff_seconds`, `_resolve_devtools_ready_timeout_seconds`,
  `_resolve_worker_timeout_seconds`, `_resolve_snap_backoff_multiplier`): unset,
  blank, malformed, zero, negative. **String and list normalisation**
  (`_resolve_chrome_proxy`, `_resolve_chrome_proxy_bypass`, `_split_no_proxy_value`):
  unset, blank, single value, comma-separated with surrounding whitespace,
  duplicate entries. **Detection** (`_detect_chrome_version`, `_resolve_user_agent`):
  a stubbed executable reporting a known version, an executable that fails to run,
  and no executable at all — each returning a usable fallback rather than raising.
  **Composite** (`_resolve_worker_timeout_details`): the returned tuple is
  consistent with the individual resolvers it composes. No function here is also
  tested by E5-3 or E1-2.
- **E5-5.** `html_to_markdown`, `sanitize_markdown`, `extract_content_as_markdown`,
  `_apply_markdown_cap`, `_build_md_suffix_url` over E3-3's fragments.
  *Verify:* structural assertions (headings preserved, code fences intact, no raw
  HTML, length within cap) fail when the corresponding transform is disabled;
  golden matching only for handcrafted fragments.
- **E5-6.** `_append_tail_text` and the encoding-cookie helpers. *Verify:*
  boundary cases at exactly the limit, one under and one over; each fails if the
  comparison operator is flipped.
- **E5-7.** Scoped to what `redact_url_credentials`, `mask_env_values`,
  `truncate_text` and `_apply_line_limit` do **today**, extending
  `test_diagnostics_masking.py` (8 tests, passing) with property-based cases. It
  merges green; every emit-boundary assertion belongs to E9-1.
  *Verify:* properties fail if a masking rule is removed; no test added here fails
  on the current tree.

---

### E6 — L2 contract tests

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E6-1** | MCP tool schema golden | PR | impl E0-2 | M |
| **E6-2** | Extract and pin the `KINDLY_DIAG` frame codec | PR | impl E2-3 | M |
| **E6-3** | `page_content` is always a string | PR | impl E0-2 | S |
| **E6-4** | Import/declaration agreement extension | PR | complete E11-5 | S |

- **E6-1.** Normalized comparison asserting names, types, required-ness, defaults,
  description presence, and `outputSchema is None` (normative — §3).
  *Verify:* renaming `num_results` fails it; a description reword does not; passes
  on mcp 1.25.0 and the newest allowed release.
- **E6-2.** **Production change (small).** Sequenced after E2-3 because both touch
  `universal_html.py`'s diagnostics path and would otherwise conflict. One
  encoder/decoder pair used by both sides.
  *Verify:* §4.3's stream cases — fragmentation, several frames per chunk, CRLF,
  EOF without newline, multi-byte split across chunks, malformed payload capping,
  oversized line truncation — each failing if its branch is removed.
  `_split_worker_diagnostics` is dead code; flag it for separate removal.
- **E6-3.** *Verify:* `page_content` is a `str` on every failure path — timeout,
  handler exception, unsupported type, empty provider result — and each assertion
  fails if the `None` conversion is removed from that branch.
- **E6-4.** Extend `test_dependency_constraints.py` to assert test-only imports
  stay within the declared `dev` extra. *Verify:* adding an undeclared import to a
  test file fails it; `anyio` is no longer imported anywhere under `tests/`.

---

### E7 — L3 subsystem tests

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E7-1** | Server over a real socket | PR | impl E3-4 | M |
| **E7-2** | `_run_worker_command` parent-side lifecycle | PR | impl E2-3, impl E3-1, impl E3-4, impl E6-2 | L |
| **E7-3** | ChromiumPool | PR | impl E3-4, merge E4-6 | L |

- **E7-1.** One canonical valid `initialize`, one security input varied per case,
  plus a no-override control returning 200. *Verify:* all eight cases of §5.1; the
  control case catches a protocol regression that would otherwise look like a
  security result.
- **E7-2.** Parent-side concerns only: spawn, stream, heartbeat, termination.
  `impl E6-2` because it is written against the final codec, not merely merged
  after it.
  *Verify:* clean run returns the child's **HTML** and parsed diagnostics; hanging
  child killed at the deadline; killed parent leaves no orphan; stderr garbage does
  not crash the parent; non-zero exit surfaces a readable error. Windows and Linux.
- **E7-3.** Slot acquisition and release, reuse, pool sizing, port allocation
  within `KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention,
  concurrency, shutdown. **Written against a locally installed Chromium** — only
  CI validation needs E4-6's image, which is why the image is a `merge`
  dependency rather than an `impl` one.
  *Verify:* after shutdown every spawned PID is gone; the port-range test fails if
  the range is ignored. Closes the repo's largest untested module.

---

### E8 — L4 product tests

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E8-1** | Wheel build, install, import-resolution harness | PR | impl E3-5 | M |
| **E8-2** | MCP session over stdio and Streamable HTTP | PR | impl E8-1, impl E3-2 | L |
| **E8-3** | Deterministic `get_content` case | PR | impl E8-1 | S |
| **E8-4** | Serper canary through the public surface | PR | impl E0-2 | M |
| **E8-5** | Extraction canary with real thresholds | PR | impl E0-2 | M |

- **E8-1.** *Verify:* asserts the server module's `__file__` is under the venv's
  `site-packages` and **not** the checkout; the assertion fails if run from the
  repo root without isolation.
- **E8-2.** *Verify:* `initialize` → `tools/list` → `tools/call` against both
  entrypoints with the zero-result fixture; **assert no Chromium process is
  created**, do not assume it.
- **E8-3.** `https://example.invalid/package-smoke.pdf`, plus the L1 guard that
  every specialized parser rejects that exact URL. *Verify:* no network call; the
  guard fails if a parser is widened — `parse_arxiv_url` already accepts
  `arxiv.org/pdf/….pdf`, which is why the host is pinned.
- **E8-4.** Serper canary via `search_web` or the `web_search` tool — **not**
  `urllib` as `test_serper_live.py` does today. Standardize on
  `KINDLY_RUN_LIVE_TESTS`. This step **owns `tests/test_serper_live.py` and
  completes its pytest migration**, so E11-1 does not also claim it; both steps
  would otherwise rewrite the same file in parallel.
  Files: `tests/test_serper_live.py`
  *Verify:* the canary asserts the reported provider, proving it exercised
  `PROVIDERS` selection; one gate variable across the suite; the file no longer
  imports `unittest` or `urllib.request`.
- **E8-5.** `tests/test_live_fetch_urls.py` currently asserts only that
  `page_content` lacks `"TimeoutError"` — it would pass on an empty page or on the
  deterministic failure note. TEST_SUITE §6.2 requires real thresholds.
  *Verify:* non-empty; above a minimum length; contains an expected anchor phrase;
  **is not** the "Could not retrieve content" note; each assertion fails when fed
  a stubbed response violating it. Needs a browser, which is why E4-13 depends on
  the runtime image.

---

### E9 — Security

**X-1 is answered** (TEST_SUITE §7.2, decided 2026-08-31), so this epic is no
longer conditional: `http`/`https` only, public addresses only for
externally-supplied URLs, `httpx` fully enforced, Chromium enforced pre-navigation
with two gaps accepted and characterized, and the upstream proxy a documented
trusted boundary.

Two consequences shape the steps below. **The `httpx` and Chromium paths get
different treatment** — Python resolution is observable, browser resolution is not.
And **the two accepted gaps still get tests**, because a gap nobody characterized
is one that gets rediscovered, and because a test asserting today's behaviour is
what flips if a policy proxy is ever added.

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E9-1** | Sanitize diagnostics at the emit boundary | PR | impl E5-7 | L |
| **E9-2** | Outbound policy function — scheme, address class, provenance | PR | impl E0-2 | M |
| **E9-3** | Enforce on the `httpx` clients, including every redirect hop | PR | impl E9-2, impl E3-6 | M |
| **E9-4** | Pre-navigation enforcement on the Chromium path | PR | impl E9-2 | M |
| **E9-5** | `httpx` rebinding and multi-address enforcement | PR | impl E9-3 | M |
| **E9-6** | Characterize the Chromium rebinding and subresource gaps | PR | impl E9-4, impl E3-6 | M |
| **E9-7** | Characterize the `CONNECT`-proxy boundary | PR | impl E9-4 | M |

- **E9-1.** **Production change**, shipped with its tests in one PR. One sanitizing
  step at the top of `Diagnostics.emit`, before the entry is appended to `entries`.
  Independent of the outbound policy.
  *Verify:* both the returned `entries` and the emitted JSON are asserted;
  `get_content("https://user:token@host/x")` leaks the token in neither; each §7.1
  policy case has a test; a test pins that sanitizing only at the writer would
  fail, so nobody relocates it later.
- **E9-2.** One pure function classifying a URL — scheme, and the address class of
  every address a host resolves to — plus the **provenance** distinction §7.2
  draws: externally-supplied URLs are restricted, operator-configured ones are not.
  No call sites yet, so it merges green.
  *Verify:* table-driven over scheme (`file:`, `data:`, `ftp:`, `chrome:` refused;
  `http`/`https` permitted) and address class (loopback, RFC1918, link-local,
  IPv6 ULA, `169.254.169.254`, IPv4-mapped IPv6), plus **mixed public/private
  record sets** — which must be refused, since §7.2 requires *every* record to be
  permitted — and dual-family answers; and both provenances for each. Each row
  fails if its branch is removed.
- **E9-3.** Applies E9-2 at the `httpx` boundary, using E3-6's seam so the resolved
  address set is observable. **Redirects are the hard part**: the clients follow
  them internally, so the check runs on each hop, not only the submitted URL.
  *Verify:* a local server issuing a public→private redirect is refused; a chain of
  two redirects is checked at both; an operator-configured URL to a private address
  is permitted; the test fails if the check is applied only to the initial URL.
- **E9-4.** Applies E9-2 to the URL handed to Chromium, **before navigation**. That
  is the whole mechanism — §7.2 accepts that the browser's own resolution and
  subresource requests are not mediated.
  *Verify:* `get_content` on a private address is refused before any browser
  starts, asserted by observing that no Chromium process is created; a public URL
  proceeds; an operator-configured URL is unaffected.
- **E9-5.** Uses E3-6's resolver seam so the race is scripted rather than real.
  *Verify:* a hostname resolving public at validation and private at connect is
  refused; a hostname returning a **mixed record set** in one answer is refused,
  which is the case needing no timing at all; deterministic across 50 consecutive
  runs.
- **E9-6.** **Characterization, not enforcement.** §7.2 accepts these two gaps, and
  this step pins them so they are visible and so closing them is observable.
  *Verify:* a permitted public page whose subresource requests a private address —
  by `<img>`, `<script>`, `<iframe>`, a stylesheet `url()` and `fetch()` — reaches
  it, and the test says so explicitly, citing §7.2's acceptance; a Chromium
  rebinding case likewise. **Each test carries a comment naming §7.3's revisit
  triggers**, so whoever sees it failing after a policy proxy lands knows the
  failure is the good outcome.
- **E9-7.** **Characterization.** With `KINDLY_CHROME_PROXY` set to a `CONNECT`
  proxy, the proxy resolves and the server cannot enforce address policy through
  it.
  *Verify:* a request whose hostname the proxy resolves to a private address
  reaches it; the test uses a **hostname, not a literal private URL**, since a
  literal address is caught by E9-4 and would not exercise the boundary; it cites
  §7.2's trusted-boundary statement.

---

### E10 — Coverage controls

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E10-1** | Classification policy test | PR | impl E2-3, impl E0-4 | M |
| **E10-2** | Non-zero coverage assertion tool, unit-tested | PR | impl E10-1 | M |
| **E10-3** | Hermetic `coverage` job — reporting only | PR | impl E10-2, merge E4-11 | M |
| **E10-4** | Worker observational coverage | PR | merge E7-2, merge E4-3 | M |
| **E10-5** | Chromium observational coverage | PR | merge E4-10 | M |
| **E10-6** | Install the assertion in each producing job | PR | merge E10-3, merge E10-4, merge E10-5 | M |
| **E10-7** | Job-summary and delta reporting | PR | merge E10-6, complete X-5 | M |
| **E10-8** | Diff-coverage gate | PR | merge E10-3 | M |
| **E10-9** | Baseline bootstrap, ratchet and reset label | PR | merge E10-8 | L |
| **E10-10** | Activate `coverage` in `ci-required` | PR | merge E10-6, merge E10-9, merge E4-10 | S |
| **E10-11** | Revisit per-module coverage floors against measured data | PR | impl E10-3 | S |

- **E10-1.** Every `src/**/*.py` in the gating scope or in `omit`, exactly once.
  *Verify:* a module in neither fails; a module in both fails.
- **E10-2.** The checker plus unit tests over **synthetic** `coverage.json`
  fixtures; not yet pointed at the real tree, so it merges green. *Verify:* a
  synthetic report with a zero-covered gating module fails; non-zero passes.
- **E10-3.** The `coverage` job of §10.3 — pinned lane, source checkout,
  `pip install --no-deps -e .`, hermetic selection. **Reporting only.**
  *Verify:* produces `coverage.xml` and `coverage.json`; the import-resolution
  assertion confirms the working tree is measured, not an installed wheel.
- **E10-4.** `parallel = true` + `patch = subprocess` in the `subsystem` job,
  local `coverage combine`, HTML/JSON artefact with `if-no-files-found: error`.
  *Verify:* `worker_runner.py` shows non-zero coverage; a forcibly killed child
  contributes nothing, and that limit is documented in the job output rather than
  hidden by an exclusion.
- **E10-5.** The same for the `chromium` job. *Verify:* `chromium_pool.py` shows
  non-zero coverage.
- **E10-6.** **Each job runs the checker against the report it produced, and only
  that report** — TEST_SUITE §10.4 rejects cross-job coverage fan-in, so there is
  no step that gathers the three. The module list is per job: `coverage` checks
  the gating-scope modules, `subsystem` checks `worker_runner.py` and
  `nodriver_worker.py`, `chromium` checks `chromium_pool.py`. One PR editing three
  job definitions, hence `M` rather than `S`.
  *Verify:* the `chromium` job's check passes now that E7-3 covers
  `chromium_pool.py`, and reverting E7-3 makes that job fail — proving it is live
  rather than vacuous; no job reads another job's artefact.
- **E10-7.** Per-module summary written to **`$GITHUB_STEP_SUMMARY`**, with a
  delta against the last successful `main` run. The job summary needs no
  `pull-requests: write` — only `actions: read` to find the previous artefact —
  which also means it behaves identically on fork PRs with no special case. A PR
  *comment* would be a different design with its own fork and security
  considerations; it is deliberately not what this step builds.
  *Verify:* the artefact lookup names the workflow and branch it reads; a
  **missing or expired** previous artefact renders the summary with deltas omitted
  rather than failing the job; the workflow declares `actions: read` and no write
  permission; the summary renders on a fork PR; the reviewer and cadence from X-5
  are recorded in the workflow.
- **E10-8.** `diff-cover --fail-under=80` from the event base SHA via
  `--diff-file`, `fetch-depth: 0`. *Verify:* a PR adding an uncovered statement in
  the gating scope fails; one adding a covered statement passes; a PR touching
  only an omitted module is unaffected.
- **E10-9.** *Verify:* bootstrap works with no baseline on the base SHA; a decrease
  fails; a decrease with the reset label passes; applying the label re-runs the
  check without a manual re-run; an unrecorded rise fails the equality check.
- **E10-10.** One line. *Verify:* `coverage` is in `ci-required.needs` and the
  aggregate is green; making a control fail turns `ci-required` red.
- **E10-11.** TEST_SUITE §13.4 defers per-module floors until a baseline exists,
  on the grounds that any number chosen earlier would be invented. E10-3 produces
  the first real measurement, so this is where that deferral is discharged rather
  than quietly forgotten.
  `impl E10-3`, not `merge`: the decision cannot be made before the measurement
  exists, so it is not authorable earlier. It is a **PR** because its artefact is a
  repository change — an amendment to `TEST_SUITE.md` §13.4 recording the measured
  per-module figures and the outcome, which closes that open item.
  *Verify:* §13.4 no longer reads as deferred; the amendment cites the figures.
  **"Do not adopt floors" is a valid outcome** provided the evidence is recorded;
  leaving §13.4 open indefinitely is not.

---

### E11 — Async migration

Batches own disjoint files; the validator enforces uniqueness, existence and that
no `unittest`-style file is left unclaimed. Sequenced after green and after the
workflow is observable — **not** after branch protection, which is an external
operation and would needlessly serialize this.

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E11-1** | Convert the search-provider tests | PR | complete E1-6, merge E4-1 | M |
| **E11-2** | Convert the content-loader tests | PR | complete E1-6, merge E4-1 | M |
| **E11-3** | Convert the scrape tests | PR | complete E1-6, merge E4-1, merge E7-2 | M |
| **E11-4** | Convert the server and resolver tests | PR | complete E1-6, merge E4-1 | M |
| **E11-5** | Migration complete | milestone | merge E11-1, merge E11-2, merge E11-3, merge E11-4 | S |

**E11-1** —
Files: `tests/test_searxng_unit.py`, `tests/test_serper_unit.py`, `tests/test_sofya_unit.py`, `tests/test_tavily_unit.py`, `tests/test_youcom_unit.py`

`tests/test_serper_live.py` is deliberately absent: E8-4 rewrites and migrates it.

**E11-2** —
Files: `tests/test_arxiv.py`, `tests/test_github_discussions.py`, `tests/test_github_issues.py`, `tests/test_stackexchange_api_client.py`, `tests/test_stackexchange_markdown.py`, `tests/test_stackexchange_parsing.py`

**E11-3** —
Files: `tests/test_nodriver_worker_sandbox.py`, `tests/test_universal_html_loader.py`, `tests/test_wikipedia.py`

**E11-4** —
Files: `tests/test_server.py`, `tests/test_search_router.py`, `tests/test_page_content_resolver.py`, `tests/test_content_resolver_universal_fallback.py`

*Verify per batch:* each converted test is seen failing before passing (inject a
fault, evidence in the PR); assertion count unchanged or higher; only the listed
files change.
**E11-5** — no diff. *Verify:* no `import unittest` and no `anyio` import remains
under `tests/`; this unblocks E6-4.

---

### E13 — Completion

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E13-1** | Suite complete | milestone | merge E10-10, merge E10-7, merge E10-11, merge E13-2, merge E12-1, complete E11-5, merge E6-1, merge E6-3, merge E6-4, merge E4-13, merge E7-1, merge E8-2, merge E8-3, merge E2-4, merge E5-5, merge E5-6, merge E9-1, merge E9-5, merge E9-6, merge E9-7 | S |
| **E13-2** | Record the risk-matrix owners in `TEST_SUITE.md` | PR | complete X-6 | S |

No diff. Exists because "every row is done" is otherwise something nobody checks:
the validator reports what is *listed*, not what is *finished*, and the E9 branch
changes which rows are applicable.

Its prerequisites are every terminal node, and
`check_terminal_reachability` **fails if any step is not transitively required by
this one** — so a future epic cannot be added and silently left out of
"complete". The check found seventeen such steps when it was introduced.
*Verify:* every step in this document is merged or complete; every external
prerequisite X-1…X-6 is resolved with its rollback note recorded; `ci-required`
carries all of `fast`, `fast-extras`, `subsystem`, `chromium`, `package`, `types`
and `coverage`; the nightly runs live and mutation jobs; `pytest` is green on both
platforms.

---

### E12 — Mutation testing

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E12-1** | Mutation configuration and its nightly job | PR | merge E4-12, merge E5-1, merge E5-2, merge E5-3, merge E5-4, merge E5-7 | M |

Configuration and job ship together — a job without its configuration is a
scheduled failure. It attaches to E4-12's nightly foundation, ahead of the live jobs, and needs **neither
live credentials nor the live jobs**.

The dependency list matches §3.2's mutation scope exactly: the URL parsers (E5-1,
E5-2), the environment resolvers (E5-3, E5-4) and diagnostics redaction (E5-7).
E5-6's accumulators are **not** in that scope, so they are not a dependency.
Installs `.[dev,mutation]` (E0-1) — the `mutation` extra is additive and carries
only `mutmut`, which declares neither `hypothesis` nor `pytest-asyncio` and so
cannot run E5-1/E5-2 on its own. Linux-only, since `mutmut` needs `fork()`.
*Verify:* completes within the nightly budget; surviving mutants publish as a
review queue, not a gate; the job is in `nightly-summary.needs`, and a failed or
skipped mutation job makes the summary fail.

---

## 5. Validation

The tables above are the source of truth. `scripts/check_plan_dag.py` parses the
`Blocked by` cells and fails on:

- loose syntax — ranges and wildcards are errors, not interpreted;
- duplicate step ids;
- references to undefined steps or prerequisites;
- a dependency kind invalid for its target's Type (`merge` on a milestone,
  `complete` on a PR);
- table rows that look like steps but did not parse;
- files claimed by two batches, claimed but absent from the tree, or
  `unittest`-style and claimed by none;
- cycles;
- steps mutating one contended region (§1.3) that are not totally ordered;
- any step not transitively required by the completion milestone E13-1;
- any external prerequisite that gates no step E13-1 requires;
- declared totals — step count, authorable count and PR-authorable count — that
  do not match the table.

```
$ python scripts/check_plan_dag.py
```

`tests/test_plan_dag.py` covers each of those failure modes and asserts the
committed plan passes. Run both on every change to this file.

---

## 6. Suggested opening

Five engineers.

**Day 1, morning** — one engineer: E0-1 then E0-2. Merge both before lunch.

**Day 1, afternoon** — streams open:

| Engineer | Starts with | Then |
|---|---|---|
| A | E2-1, E2-2 | E2-3, E2-4 |
| B | E1-1, E1-2 | E1-3, E1-5 |
| C | E3-1, E3-4 | E7-1, E7-2 |
| D | E0-3, E4-12 | E0-4, E4-1 |
| E | E5-1, E5-2 | E6-1, E6-3 |

E1-4 goes to engineer B as soon as A's E2-1 lands; E2-3 waits on it. E4-1 is
authored early but merges with E1-6, and E4-2 follows immediately — the gate is on
within a day of green. Coverage work (E10-1) waits on E2-3. E3-2, E3-3, E3-5,
E5-3, E5-6, E5-7, E8-4 are unassigned backlog. With `pr_authorable` steps open
from day one (§2), the constraint on throughput is people, not the graph.

In parallel, the maintainer works **X-1** — the only prerequisite blocking steps
from being *written*, and by far the largest in downstream scope: it decides three
things (§3), and under a restrict policy E9-0 must re-plan the branch before any
of it can start. Then X-2, X-3, X-4, X-5 and X-6.

**Protect E2-3.** One refactor unblocks the L3 worker stream, the codec
extraction, the coverage classification and all of E10, and it reads like
something that can wait.
