# Test suite implementation plan

Executable plan for `.system_design/TEST_SUITE.md`. Every step traces to a section
of that document; this file adds sequencing, dependencies and acceptance criteria,
not new design.

---

## 1. Rules

### 1.1 Two phases, because the repository starts red

The repository has 11 known failures (TEST_SUITE §1.1). "Every merged step leaves
the suite green" cannot hold until E1-6 restores it, so the invariant is stated in
two phases and the CI activation order follows from it:

| Phase | Invariant | CI |
|---|---|---|
| **Before E1-6** | No PR may introduce a failure beyond the recorded per-OS baseline. The baseline count may only go down. | The workflow runs and reports. **Nothing is a required check.** |
| **After E1-6** | Every merged PR is fully green. | `ci-required` becomes required (E4-5); further jobs are activated one at a time. |

A workflow that runs a failing suite is not made acceptable by branch protection
being off — it is still red on `main`. So E4-1 lands the workflow in
**reporting-only** mode, and every activation is its own step.

### 1.2 Infrastructure → tests → activation

Never activation first. A marker-selected job enabled before its tests exist is
red by construction under `--min-selected`, and the tests that would fix it then
cannot merge past the job they need to fix. Every required job therefore has a
separate activation step whose only content is adding it to `ci-required.needs`.

### 1.3 Red and green ship together

A test-only PR that fails cannot merge under the gate. A new assertion and the
code satisfying it are one PR. Where a test must be seen failing first — and it
must — that happens in the author's tree, evidenced in the PR description.

### 1.4 Dependency kinds

| Kind | Meaning |
|---|---|
| `impl` | cannot be written until the other step exists |
| `merge` | can be written independently; cannot merge until the other lands |
| `complete` | the other is not a PR — a decision, milestone or operation |

A step blocked only by `merge` or `complete` is fully parallelisable today.

### 1.5 Step types

`PR` (a reviewable change), `milestone` (a state assertion, no diff),
`decision` (a recorded choice), `operation` (a repository or infrastructure
action). Non-PR steps are depended on with `complete`.

### 1.6 Conventions

- One PR per `PR` step. If it needs two, it is two steps.
- Useful and correct on merge; no later step required to make it so.
- An acceptance check a reviewer can run, naming a fault to inject where the step
  adds tests.
- Sizes: S under half a day, M half to two days, L two to five days.

---

## 2. Sequencing for parallelism

Only **E0-1 and E0-2** are serial — a dependency declaration and a pytest config
block, both S. Everything else fans out from them; the validator reports how many
steps that is. Each stream starts on its own narrow dependency, not on a wave.

| # | Stream | Starts after | Steps |
|---|---|---|---|
| 1 | Worker seam & protocol | E0-2 | E2-1, E2-2, E2-3, E2-4 |
| 2 | Baseline restoration | E0-2 | E1-1, E1-2, E1-3, E1-5 |
| 3 | Fixtures & corpus | E0-2 | E3-1 … E3-5 |
| 4 | CI skeleton | E0-3 | E4-1, E4-9 |
| 5 | L1 parsers | E0-1 | E5-1, E5-2 |
| 6 | L1 resolvers & accumulators | E0-2 | E5-3, E5-6, E5-7 |
| 7 | L2 contracts | E0-2 | E6-1, E6-3 |
| 8 | L4 live canary | E0-2 | E8-4 |
| 9 | Coverage configs | E0-1 | E0-4 |

Opening later, as prerequisites land: L3 server (E3-4), L3 worker (E2-3 + E3-1),
L4 product (E3-2 + E3-5), coverage controls (E2-3 → E10-1), ChromiumPool (E3-4),
security sanitizer (E5-7), transforms (E3-3).

---

## 3. External prerequisites

Not reviewable PRs. Each needs an owner, evidence of completion, and a **rollback
note recorded before any step that depends on it activates**.

| ID | Prerequisite | Blocks | Owner |
|---|---|---|---|
| **X-1** | **Outbound request policy decision** (TEST_SUITE §13.1). Is fetching private-network addresses intentional? Artefact: an approved amendment to TEST_SUITE §7.2 stating the policy. | E9-2, E9-3 | maintainer |
| **X-2** | Repository admin authority — branch protection, require a PR, no bypass actors | E4-5 | repo admin |
| **X-3** | Container registry — registry choice, publish credentials as Actions secrets, and a decision on whether fork PRs may pull the image | E4-6 | repo admin |
| **X-4** | Live-query budget owner — billing authorisation for nightly spend and a named person alerted on nightly failure. `SERPER_API_KEY` already exists. | E4-10 | maintainer |

**X-1 blocks two steps from being written.** X-2, X-3 and X-4 block activation or
infrastructure only; every test they eventually gate can be written and merged
first. `outputSchema is None` is **normative for this implementation** (E6-1); if
TEST_SUITE §13.2 later changes it, that is a new step, not a pending decision here.

---

## 4. Epics

### E0 — Bootstrap

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E0-1** | Test dependencies and the `ratchet` extra | PR | — | S |
| **E0-2** | `[tool.pytest.ini_options]` with markers | PR | impl E0-1 | S |
| **E0-3** | `--min-selected` collection guard | PR | impl E0-2 | S |
| **E0-4** | The three coverage configs | PR | impl E0-1 | S |

- **E0-1.** `pytest-asyncio`, `hypothesis`, `coverage`, `diff-cover`, `mypy` at
  §10.2's bounds; a `ratchet` extra; `requirements-ratchet.txt` per §10.4.
  *Verify:* `pip install -e .[dev]` succeeds on both platforms; the lockfile has no
  local path or VCS URL; a fresh venv from it runs all five tools.
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
  *Verify:* both per-OS numbers committed into §1.1, replacing the prediction; a
  `BASELINE.md` or equivalent records them for §1.1's phase-one invariant.
- **E1-2.** Direct tests of `_build_chromium_launch_args`,
  `_resolve_sandbox_enabled`, `_resolve_browser_executable_path`,
  `_resolve_start_retry_attempts` with §3.1's ambient-state seams.
  *Verify:* each fails when its resolver's default is inverted; `--no-sandbox`,
  root detection and executable discovery all survive; no new test calls
  `_fetch_html`.
- **E1-3.** Sequenced after E1-2: both edit the same eight stale tests. E1-2
  removes the flag half; E1-3 rewrites what remains with autospec doubles around
  the browser-connect call. **These stay at this layer permanently** —
  `_fetch_html` is the child process's own function and owns browser startup,
  connect retry and profile cleanup; `_run_worker_command` is parent-side.
  *Verify:* retry-count, terminate-failed-attempt, non-retryable-not-retried and
  profile-cleanup pass; adding a required argument to the patched callable makes
  them fail rather than silently pass.
- **E1-4.** Rebuild `_FakeProc` as E2-1's typed fake.
  *Verify:* all three pass; deleting `stdout` from the fake fails both mypy and
  the runtime conformance test.
- **E1-5.** *Verify:* passes on both platforms; each retained case (explicit,
  malformed, zero, negative, `num_results` limit) fails if the clamp is removed.
- **E1-6.** No diff. *Verify:* **0 failed** on Windows and Linux, recorded in the
  milestone issue. Phase two of §1.1 begins here.

---

### E2 — Worker seam and protocol

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E2-1** | `WorkerProcess` Protocol, typed fake, mypy harness — test-only | PR | impl E0-1 | M |
| **E2-2** | Extract `_build_worker_command` | PR | impl E0-2 | S |
| **E2-3** | Extract `_run_worker_command` into `scrape/worker_runner.py` | PR | impl E2-2, merge E1-4 | M |
| **E2-4** | Annotate `_run_worker_command` with `WorkerProcess` | PR | impl E2-1, impl E2-3 | S |

- **E2-1.** No production change. `@runtime_checkable` Protocol over the surface
  production already consumes from `asyncio.subprocess.Process`; a concrete fully
  annotated fake (not `AsyncMock`); the forcing assignment; `disallow_any_expr` on
  that surface; the runtime contract test.
  **The negative fixture is excluded from the ordinary mypy target.** A file that
  must fail type-checking cannot sit in the path the `types` job checks, or that
  job is red forever. It lives under `tests/typing_negative/`, excluded in mypy
  config, and is exercised by a harness that shells out to mypy, asserts a
  non-zero exit, and asserts the *specific* diagnostic code.
  *Verify:* the harness fails if the negative fixture starts type-checking
  cleanly; removing `stdout` from the fake fails mypy and the conformance test; a
  test documents that `isinstance` alone does not catch a wrong `wait()`
  signature.
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
- **E2-4.** **Production change.** *Verify:* mypy checks the production signature
  against the Protocol; changing the Protocol without the function fails.

---

### E3 — Fixtures and corpus

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E3-1** | Fixture child process script | PR | impl E0-2 | M |
| **E3-2** | Local SearXNG-contract HTTP server fixture | PR | impl E0-2 | M |
| **E3-3** | HTML corpus scaffolding and governance | PR | impl E0-2 | M |
| **E3-4** | Anti-flake harness helpers | PR | impl E0-2 | M |
| **E3-5** | `tests/package/` and its marker policy test | PR | impl E0-2 | S |

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
  failure. *Verify:* a hanging fixture child is killed at the deadline and its PID
  tree is gone; cleanup never matches processes by name.
- **E3-5.** *Verify:* a file added there without `@pytest.mark.package` fails the
  policy test; source-checkout jobs pass `--ignore=tests/package`.

---

### E4 — CI and enforcement

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E4-1** | Workflow skeleton, broad job, `ci-required` — reporting only | PR | impl E0-3 | M |
| **E4-2** | Split into the normative `fast` and `subsystem` jobs | PR | merge E1-6, merge E4-1 | M |
| **E4-3** | `fast-extras` job | PR | merge E4-2 | S |
| **E4-4** | `types` job | PR | merge E2-1, merge E4-1 | S |
| **E4-5** | Enable branch protection | operation | complete X-2, merge E4-2, merge E4-3, merge E4-4 | S |
| **E4-6** | Build and publish the test-runtime image | PR | complete X-3 | L |
| **E4-7** | Activate the `chromium` job | PR | merge E7-3, merge E4-6 | S |
| **E4-8** | Activate the `package` job | PR | merge E8-1 | S |
| **E4-9** | Nightly workflow foundation | PR | impl E0-3 | S |
| **E4-10** | Add the live jobs to the nightly | PR | merge E8-4, complete X-4 | M |

- **E4-1.** §10.3's complete trigger list; one job selecting
  `--ignore=tests/package -m "not live and not chromium and not package"` on both
  platforms — deliberately **including** `subsystem`, per TEST_SUITE §8B; and
  `ci-required` with `if: always()` asserting every dependency is `success`.
  **Not a required check yet** (§1.1). *Verify:* a failing test makes `ci-required`
  red; a **skipped** dependency also makes it red; a fork PR triggers the workflow.
- **E4-2.** Narrow the broad job to the normative selector
  `-m "not live and not subsystem and not chromium and not package"` over the
  Python 3.13/3.14 × mcp {min, max} matrix, and add `subsystem` selecting
  `-m "subsystem and not chromium and not live"` on both platforms.
  *Verify:* every test that ran in E4-1's broad job runs in exactly one of the two;
  `--min-selected` floors are set from the counts observed in E4-1.
- **E4-3.** `fast-extras`: the `fast` selector with `pdf-advanced` installed,
  Python 3.13 only. *Verify:* the job installs the extras and a PDF-path test that
  skips without them runs here; it is in `ci-required.needs`.
- **E4-4.** *Verify:* the negative fixture directory is excluded; the job is green
  on merge; introducing an `Any` on the Protocol surface makes it red.
- **E4-5.** No diff to the repository. *Verify:* a PR with a red `ci-required`
  cannot be merged, including by an administrator; the rollback procedure is
  recorded in the operation ticket.
- **E4-6.** Python, Chromium, system deps, **no application code**, from a pinned
  base digest with pinned package versions. Not wired into CI.
  *Verify:* a manual workflow run pulls by digest, installs a wheel into it and
  launches Chromium; the digest is recorded in the repository.
- **E4-7.** *Verify:* the job runs E7-3's tests with a real `--min-selected`; the
  wheel-resolution assertion holds; it is in `ci-required.needs`.
- **E4-8.** `tests/package -m package`, Python 3.13 × mcp {min, max}.
  *Verify:* as E4-7, against E8-1's tests.
- **E4-9.** A `schedule`-triggered workflow with `workflow_dispatch` and a
  `nightly-summary` aggregator, and **no jobs yet**. Exists so mutation and live
  work can attach independently. *Verify:* a manual dispatch runs and the summary
  reports zero jobs without failing.
- **E4-10.** `live-serper` and `live-extraction`. *Verify:* with the secret removed
  the job **fails** rather than skipping; `KINDLY_RUN_LIVE_TESTS=1` is set in the
  job env; the summary reports skip counts; fork PRs never receive the secret.

---

### E5 — L1 component and property tests

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E5-1** | Parser mutual-exclusivity property | PR | impl E0-1 | M |
| **E5-2** | Per-parser identifier preservation and rejection | PR | impl E0-1 | M |
| **E5-3** | Environment resolver tables | PR | impl E0-2 | M |
| **E5-4** | Launch-arg and sandbox resolver coverage | PR | impl E1-2 | S |
| **E5-5** | Markdown transforms against the corpus | PR | impl E3-3 | M |
| **E5-6** | Text accumulators and encoding-cookie helpers | PR | impl E0-2 | S |
| **E5-7** | Redaction helper units — existing behaviour only | PR | impl E0-2 | M |

- **E5-1.** Hypothesis over generated URLs. *Verify:* fails if any parser's
  matching is widened to overlap another; the shrunk counterexample is readable.
- **E5-2.** For each of the five parsers: a generated URL built from known
  identifiers returns exactly those identifiers; a rejected URL raises that
  parser's own error type; rejection is stable across trailing slashes, scheme
  case and query order. *Verify:* each property fails if its parser's group
  extraction is off by one, and if the parser is changed to raise bare `Exception`.
- **E5-3.** `_resolve_transport`, `_resolve_host_port`,
  `_resolve_tool_total_timeout_seconds`, `_resolve_web_search_max_concurrency`,
  `_resolve_transport_security`, `_cors_origin_regex`, plus `_parse_port_range` and
  the `_resolve_*` families in `chromium_pool.py` and `nodriver_worker.py`.
  *Verify:* each parameterized over unset, blank, valid, malformed, zero, negative
  and out-of-range; every case fails if its branch is removed.
- **E5-4.** Extends E1-2 to the resolvers it did not need. *Verify:* every
  `_resolve_*` in `nodriver_worker.py` has at least one test that fails when its
  default is inverted.
- **E5-5.** `html_to_markdown`, `sanitize_markdown`, `extract_content_as_markdown`,
  `_apply_markdown_cap`, `_build_md_suffix_url` over E3-3's fragments.
  *Verify:* structural assertions (headings preserved, code fences intact, no raw
  HTML, length within cap) fail when the corresponding transform is disabled;
  golden matching is used only for handcrafted fragments.
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
| **E6-4** | Import/declaration agreement extension | PR | merge E11-5 | S |

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
| **E7-2** | `_run_worker_command` parent-side lifecycle | PR | impl E2-3, impl E3-1, impl E3-4, merge E6-2 | L |
| **E7-3** | ChromiumPool | PR | impl E3-4, merge E4-6 | L |

- **E7-1.** One canonical valid `initialize`, one security input varied per case,
  plus a no-override control returning 200. *Verify:* all eight cases of §5.1; the
  control case catches a protocol regression that would otherwise look like a
  security result.
- **E7-2.** Parent-side concerns only: spawn, stream, heartbeat, termination.
  Depends on E6-2 so it is written against the final codec.
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
| **E8-4** | Live canaries through the public surface | PR | impl E0-2 | M |

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
  `KINDLY_RUN_LIVE_TESTS`. *Verify:* the canary asserts the reported provider,
  proving it exercised `PROVIDERS` selection; one gate variable across the suite.

---

### E9 — Security

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E9-1** | Sanitize diagnostics at the emit boundary | PR | impl E5-7 | L |
| **E9-2** | Outbound URL validation tests | PR | complete X-1, impl E0-2 | M |
| **E9-3** | Redirect and DNS-rebinding tests | PR | complete X-1, impl E3-4 | M |

- **E9-1.** **Production change**, shipped with its tests in one PR. One sanitizing
  step at the top of `Diagnostics.emit`, before the entry is appended to `entries`.
  *Verify:* both the returned `entries` and the emitted JSON are asserted;
  `get_content("https://user:token@host/x")` leaks the token in neither; each §7.1
  policy case has a test; a test pins that sanitizing only at the writer would
  fail, so nobody relocates it later.
- **E9-2.** Table-driven over scheme (`file:`, `data:`, `ftp:`, `chrome:`) and
  address class (loopback, RFC1918, link-local, IPv6 ULA, `169.254.169.254`),
  asserting whatever X-1 decided. *Verify:* each row fails if its branch of the
  policy is removed; the test file cites the X-1 artefact so the expected
  behaviour is traceable.
- **E9-3.** A local server issuing a public→private redirect, and a hostname whose
  resolution changes between validation and connect. *Verify:* both fail if the
  check is applied only at the initial URL.

---

### E10 — Coverage controls

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E10-1** | Classification policy test | PR | impl E2-3, impl E0-4 | M |
| **E10-2** | Non-zero coverage assertion tool, unit-tested | PR | impl E10-1 | M |
| **E10-3** | Hermetic `coverage` job — reporting only | PR | impl E10-2, merge E4-2 | M |
| **E10-4** | Observational L3 reporting, and the omitted-module assertion | PR | merge E10-3, merge E7-2, merge E4-7 | M |
| **E10-5** | Diff-coverage gate | PR | impl E10-3 | M |
| **E10-6** | Baseline bootstrap, ratchet and reset label | PR | impl E10-5 | L |
| **E10-7** | Activate `coverage` in `ci-required` | PR | merge E10-4, merge E10-6 | S |

- **E10-1.** Every `src/**/*.py` in the gating scope or in `omit`, exactly once.
  *Verify:* a module in neither fails; a module in both fails.
- **E10-2.** The checker plus unit tests over **synthetic** `coverage.json`
  fixtures; not yet pointed at the real tree, so it merges green. *Verify:* a
  synthetic report with a zero-covered gating module fails; non-zero passes.
- **E10-3.** The `coverage` job of §10.3 — pinned lane, source checkout,
  `pip install --no-deps -e .`, running the hermetic selection. **Reporting only,
  not in `ci-required`.** *Verify:* it produces `coverage.xml` and
  `coverage.json`; the import-resolution assertion confirms the working tree is
  measured, not an installed wheel.
- **E10-4.** Publishes the `subsystem` and `chromium` HTML/JSON reports with
  `if-no-files-found: error`, posts the per-module summary to the PR, and wires
  E10-2's assertion against both the hermetic and observational reports.
  *Verify:* the omitted-module assertion passes now that E7-3 covers
  `chromium_pool.py`, and reverting E7-3 makes it fail — proving it is live rather
  than vacuous.
- **E10-5.** `diff-cover --fail-under=80` from the event base SHA via
  `--diff-file`, `fetch-depth: 0`. *Verify:* a PR adding an uncovered statement in
  the gating scope fails; one adding a covered statement passes; a PR touching
  only an omitted module is unaffected.
- **E10-6.** *Verify:* bootstrap works with no baseline on the base SHA; a decrease
  fails; a decrease with the reset label passes; applying the label re-runs the
  check without a manual re-run; an unrecorded rise fails the equality check.
- **E10-7.** *Verify:* `coverage` appears in `ci-required.needs` and the aggregate
  is green on merge; making a control fail turns `ci-required` red.

---

### E11 — Async migration

Batches own disjoint files; the validator enforces it. Sequenced after E1-6 and
after the workflow is observable (E4-1) — **not** after branch protection, which is
an external operation and would needlessly serialize this.

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E11-1** | Convert the search-provider tests | PR | merge E1-6, merge E4-1 | M |
| **E11-2** | Convert the content-loader tests | PR | merge E1-6, merge E4-1 | M |
| **E11-3** | Convert the scrape tests | PR | merge E1-6, merge E4-1, merge E7-2 | M |
| **E11-4** | Convert the server and resolver tests | PR | merge E1-6, merge E4-1 | M |
| **E11-5** | Migration complete | milestone | merge E11-1, merge E11-2, merge E11-3, merge E11-4 | S |

**E11-1** —
Files: `tests/test_searxng_unit.py`, `tests/test_serper_unit.py`, `tests/test_serper_live.py`, `tests/test_sofya_unit.py`, `tests/test_tavily_unit.py`

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

### E12 — Mutation testing

| ID | Step | Type | Blocked by | Size |
|---|---|---|---|---|
| **E12-1** | Mutation configuration and its nightly job | PR | merge E4-9, merge E5-1, merge E5-2, merge E5-3, merge E5-6, merge E5-7 | M |

Configuration and job ship together — a job without its configuration is a
scheduled failure. It attaches to E4-9's nightly foundation and needs **neither
live credentials nor the live jobs**. Scoped to the pure-logic modules (§3.2);
Linux-only, since `mutmut` needs `fork()`.
*Verify:* completes within the nightly budget; surviving mutants publish as a
review queue, not a gate.

---

## 5. Validation

The tables above are the source of truth. `scripts/check_plan_dag.py` parses the
`Blocked by` cells, rejects loose syntax rather than interpreting it, and fails on
undefined references, duplicate ids, wrong dependency kinds for external
prerequisites, overlapping file ownership, and cycles:

```
$ python scripts/check_plan_dag.py
```

`tests/test_plan_dag.py` covers the validator itself — ranges, wildcards,
duplicates, unknown externals, cycles, steps downstream of a cycle,
backward-numbered dependencies, chain depth and file-ownership collisions — and
asserts the committed plan passes. Run both on every change to this file.

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
| D | E0-3, E4-1 | E0-4, E4-9 |
| E | E5-1, E5-2 | E6-1, E6-3 |

E1-4 goes to engineer B as soon as A's E2-1 lands; E2-3 waits on it. Coverage work
(E10-1) waits on E2-3 and is not day-one work. E3-2, E3-3, E3-5, E5-3, E5-6, E5-7,
E8-4 are unassigned backlog.

In parallel and off the critical path, the maintainer works **X-1** — the only
prerequisite that stops steps being written — then X-2, X-3 and X-4.

**Protect E2-3.** One refactor unblocks the L3 worker stream, the codec extraction,
the coverage classification and all of E10, and it reads like something that can
wait.
