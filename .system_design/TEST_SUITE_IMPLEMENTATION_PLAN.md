# Test suite implementation plan

Executable plan for `.system_design/TEST_SUITE.md`. Every step traces to a section
of that document; this file adds sequencing, dependencies and acceptance criteria,
not new design.

**Status:** ready to start.

---

## 1. Rules

### 1.1 Every merged step leaves the repository green

This is the constraint that shapes everything below, and it is not negotiable:
the whole point of the design is that a red `main` stops being normal. A step that
knowingly leaves a required check failing is not a step, it is an outage with a
ticket number.

Two consequences:

- **Red and green live in the same PR.** A test-only PR that fails cannot merge
  under the gate, so a new assertion and the code that satisfies it ship together.
  Where a test must be seen failing first — and it must — that happens in the
  author's working tree, evidenced in the PR description, not on `main`.
- **New required jobs follow infrastructure → tests → activation.** Never
  activation first. Enabling a marker-selected job before its tests exist makes it
  red, and `--min-selected` guarantees it; the tests that would fix it then cannot
  merge past the job they need to fix. That deadlock is easy to create and
  expensive to unwind, so activation is always its own step.

### 1.2 Three kinds of dependency

Collapsing these is what produced the cycles in the first draft of this plan:

| Kind | Meaning |
|---|---|
| **impl** | cannot be written until the other step exists |
| **merge** | can be written independently, but cannot merge until the other lands |
| **activate** | can merge, but the enforcement it configures cannot be switched on |

A step blocked only by `activate` is fully parallelisable.

### 1.3 Step conventions

- **Atomic.** One reviewable PR. If it needs two, it is two steps.
- **Self-contained.** Useful and correct on merge, with no later step required to
  make it so.
- **Testable.** An acceptance check a reviewer can run. For steps adding tests,
  the check names a fault to inject — a test never seen red proves nothing.
- **Sizes.** S = under half a day, M = half to two days, L = two to five days.
- Steps changing `src/` say so.

---

## 2. Sequencing for parallelism

**Bootstrap is hours, not a wave.** E0-1 and E0-2 are the only genuinely serial
work — a dependency declaration and a pytest config block. Everything else fans
out from them. An earlier draft of this plan proposed a three-day "Wave 1" with
two of five engineers idle, which contradicted the document's own goal; the
correct read is that after E0-2 lands, **nine streams can start the same day.**

```
hour 0    E0-1 ── E0-2          1 engineer
hour 4    ████████████████████  9 streams open
```

Each stream starts when *its own* narrow dependency is satisfied, not when some
notional wave completes.

| # | Stream | Starts after | Steps |
|---|---|---|---|
| 1 | Worker seam & protocol | E0-2 | E2-1 → E2-2 → E2-3 → E2-4 |
| 2 | Baseline restoration | E0-2 (E1-4 needs E2-1) | E1-1…E1-6 |
| 3 | Fixtures & corpus | E0-2 | E3-1…E3-5 |
| 4 | CI skeleton | E0-2 → E0-3 | E4-1, E4-2 |
| 5 | Coverage tooling | E0-2 → E0-4 | E10-1, E10-2 |
| 6 | L1 parsers | E0-1 | E5-1, E5-2 |
| 7 | L1 resolvers & accumulators | E0-2 | E5-3, E5-6, E5-7 |
| 8 | L2 contracts | E0-2 | E6-1…E6-3 |
| 9 | L3 server socket | E3-4 | E7-1 |

Streams that open later, as their prerequisites land: L1 transforms (E3-3), L3
worker (E2-3 + E3-1), L4 product (E3-2 + E3-5), ChromiumPool (E4-5a), security
sanitizer (E5-7).

---

## 3. External prerequisites

These are not reviewable PRs. They need an owner, a decision and evidence of
completion, and several block activation steps.

| ID | Prerequisite | Blocks | Owner |
|---|---|---|---|
| **X-1** | **Outbound request policy decision** — TEST_SUITE §13.1. Is fetching private-network addresses intentional? | E9-2, E9-3, E9-4 | maintainer |
| **X-2** | **Repository admin authority** — enable branch protection, require a PR, remove bypass actors. §10.4's push-to-`main` rule is void without it | E4-2 (activate) | repo admin |
| **X-3** | **Container registry** — registry choice, publish credentials as Actions secrets, and a policy decision on whether fork PRs may pull the image | E4-5a | repo admin |
| **X-4** | **Live-query budget owner** — `SERPER_API_KEY` exists; still needed are billing authorisation for nightly spend and a named person alerted when the nightly fails | E4-7 (activate) | maintainer |
| **X-5** | Tool result schema decision — §13.2. Changes E6-1's golden, which currently pins `outputSchema is None` | — (content only) | maintainer |

Only **X-1** stops work from starting. X-2, X-3 and X-4 block *activation* steps,
so the tests they gate can be written and merged first.

Each needs a **rollback note** before activation: how to revert branch protection,
how to pin back to a previous image digest, how to disable the nightly.

---

## 4. Epics

### E0 — Bootstrap

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-1** | Test dependencies and the `ratchet` extra | — | S |

`pytest-asyncio`, `hypothesis`, `coverage`, `diff-cover`, `mypy` at §10.2's bounds;
a `ratchet` extra; `requirements-ratchet.txt` per §10.4.
**Verify:** `pip install -e .[dev]` succeeds on both platforms; the lockfile
contains no local path or VCS URL; a fresh venv from it runs all five tools.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-2** | `[tool.pytest.ini_options]` with markers | impl E0-1 | S |

**Verify:** `pytest --markers` lists all seven; an unregistered marker fails
collection; existing pass/fail counts are unchanged.

*Everything below can start once E0-2 lands.*

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-3** | `--min-selected` collection guard | impl E0-2 | S |

**Verify:** `-m <unmatched> --min-selected=1` exits 4 with the count in the
message; a real failure with a satisfied minimum exits 1; a satisfied minimum
passes. `pytest_collection_modifyitems` is the wrong hook — it runs before mark
deselection and never fires.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-4** | The three coverage configs | impl E0-1 | S |

**Verify:** with `.coveragerc`, `coverage json` includes
`scrape/chromium_pool.py` at zero — the observable proving `source_pkgs` works.
With `.coveragerc-gate` it is absent instead.

---

### E1 — Restore green

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-1** | Measure and record the Linux baseline | impl E0-2 | S |

§1.1 predicts 12 POSIX failures *from source, unmeasured*.
**Verify:** both per-OS numbers committed into §1.1, replacing the prediction.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-2** | Move sandbox flag/default assertions to L1 | impl E0-2 | M |

Direct tests of `_build_chromium_launch_args`, `_resolve_sandbox_enabled`,
`_resolve_browser_executable_path`, `_resolve_start_retry_attempts` with §3.1's
ambient-state seams.
**Verify:** each fails when its resolver's default is inverted; `--no-sandbox`,
root detection and executable discovery all survive; no new test calls
`_fetch_html`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-3** | Keep retry/cleanup orchestration at `_fetch_html` | impl **E1-2** | M |

Explicitly sequenced after E1-2: both edit the same eight stale tests, and
concurrent PRs would disagree about which assertions each owns. E1-2 removes the
flag half; E1-3 rewrites what remains with autospec doubles around the
browser-connect call.
**Verify:** retry-count, terminate-failed-attempt, non-retryable-not-retried and
profile-cleanup all pass; adding a required argument to the patched callable makes
them fail rather than silently pass.

**These tests stay at this layer permanently.** `_fetch_html` is the *child*
process's own function — it owns browser startup, connect retry and profile
cleanup. `_run_worker_command` is the *parent* side — spawn, streams, heartbeat,
termination. They are different behaviours, and an earlier draft of this plan
wrongly proposed migrating one onto the other.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-4** | Repair the three loader tests | impl **E2-1** | M |

Rebuild `_FakeProc` as the typed fake from E2-1.
**Verify:** all three pass; deleting `stdout` from the fake fails both mypy and the
runtime conformance test — the regression that started all this.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-5** | Rewrite the concurrency test OS-neutral | impl E0-2 | S |

**Verify:** passes on both platforms; each retained case (explicit, malformed,
zero, negative, `num_results` limit) fails if the clamp is removed.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-6** | Suite green on both platforms | merge E1-1…E1-5 | S |

**Verify:** **0 failed** on Windows and Linux. Gates E4-2.

---

### E2 — Worker seam and protocol

Ordered to break the cycle the first draft contained: the Protocol was annotating
a function that did not exist yet, while the extraction depended on tests that
depended on the Protocol.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-1** | `WorkerProcess` Protocol, typed fake, mypy config — **test-only** | impl E0-1 | M |

No production change. `@runtime_checkable` Protocol describing the surface
production *already* consumes from `asyncio.subprocess.Process`, a concrete fully
annotated fake (not `AsyncMock`), the forcing assignment, `disallow_any_expr` on
that surface, and the runtime contract test.
**Verify:** the negative fixture — a deliberately `Any`-typed double — **fails**
mypy; removing `stdout` from the fake fails mypy and the conformance test; a test
documents that `isinstance` alone does *not* catch a wrong `wait()` signature, so
nobody later removes the static check as redundant.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-2** | Extract `_build_worker_command` | impl E0-2 | S |

**Production change.**
**Verify:** an L1 test asserts the command shape including
`-m kindly_web_search_mcp_server.scrape.nodriver_worker`; existing tests unchanged.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-3** | Extract `_run_worker_command` into `scrape/worker_runner.py` | impl E2-2, merge **E1-4** | M |

**Production change, and the highest-leverage step in the plan** — it opens the L3
worker stream and makes the coverage classification expressible (§10.4, §11.2).
E1-4 must land first so a green characterization baseline exists to refactor
against.
**Verify:** `fetch_html_via_nodriver` behaviour unchanged (E1-4's tests still
pass); `_run_worker_command` importable and accepts an arbitrary command; no
`command=` parameter on any public function; `universal_html.py` retains the
Markdown-probe path and no subprocess management.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-4** | Annotate `_run_worker_command` with `WorkerProcess` | impl E2-1, E2-3 | S |

**Production change.** The annotation E2-1 could not apply because the function
did not exist.
**Verify:** mypy checks the production signature against the Protocol; changing
the Protocol without changing the function fails.

---

### E3 — Fixtures and corpus

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-1** | Fixture child process script | impl E0-2 | M |

Emits known `KINDLY_DIAG` frames, can hang, can exit non-zero, can write garbage
to stderr.
**Verify:** a smoke test drives each mode. **Readiness is polled with a generous
hard timeout (≥30 s), not asserted against a fixed startup budget** — a 500 ms
threshold, as an earlier draft proposed, is a flake generator on loaded CI runners
and under antivirus process-start delays. Startup duration is recorded as
telemetry in the test output, never as a pass condition.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-2** | Local SearXNG-contract HTTP server fixture | impl E0-2 | M |

Ephemeral port, readiness handshake, configurable result set including **zero
results** (§6.1).
**Verify:** `search_searxng` parses its responses; with `SEARXNG_BASE_URL` pointed
at it and higher-priority provider variables cleared, `search_web` selects
SearXNG; zero-result mode returns `[]`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-3** | HTML corpus scaffolding and governance | impl E0-2 | M |

`tests/corpus/html/`, the `.meta.json` sidecar schema, and a policy test for
§3.3.
**Verify:** the policy test fails on a snapshot with no sidecar; over 200 KB; a
sidecar missing **source URL, capture date, or licence/rationale**; and content
matching **sanitation patterns — `Set-Cookie`, `Authorization`, bearer-shaped
tokens, email addresses, known analytics script bodies**. Seed with at least three
handcrafted fragments.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-4** | Anti-flake harness helpers | impl E0-2 | M |

§5.4: readiness handshake, ephemeral ports, isolated profile directories, PID-tree
cleanup keyed on spawned PIDs, child log capture on failure.
**Verify:** a hanging fixture child is killed at the deadline and its PID tree is
gone; cleanup never matches processes by name.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-5** | `tests/package/` and its marker policy test | impl E0-2 | S |

**Verify:** a file added there without `@pytest.mark.package` fails the policy
test; source-checkout jobs pass `--ignore=tests/package`.

---

### E4 — CI and enforcement

Every activation step is separate from the work it enforces (§1.1).

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-1** | Workflow skeleton, first job, `ci-required` | impl E0-3 | M |

§10.3's complete trigger list, one job selecting
`--ignore=tests/package -m "not live and not chromium and not package"` on both
platforms, and `ci-required` with `if: always()` asserting every dependency is
`success`.
**Verify:** a failing test makes `ci-required` red; a **skipped** dependency also
makes it red — the failure mode `needs` alone misses; a fork PR triggers the
workflow.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-2** | Branch protection | activate E4-1, **E1-6**, **X-2** | S |

**Verify:** a PR with a red `ci-required` cannot be merged, including by an
administrator. Record the rollback procedure.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-3** | Activate the `types` job | activate E2-1 | S |
| **E4-4** | Activate the `subsystem` job, both platforms | activate **E7-2** | S |
| **E4-6** | Activate the `package` job with the mcp {min,max} axis | activate **E8-1** | S |

Each: added to the workflow and `ci-required.needs`, with a real `--min-selected`
floor.
**Verify:** a typo'd selector fails the job; the floor matches the tests that
actually exist at that moment.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-5a** | Build, publish and smoke-test the runtime image | impl E0-1, **X-3** | L |

Python, Chromium, system deps, **no application code**, from a pinned base digest
with pinned package versions. **Not wired into CI as a required job.**
**Verify:** a manual workflow run pulls the image by digest, installs a wheel into
it, and launches Chromium successfully; the digest is recorded.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-5b** | Activate the `chromium` job | activate **E7-4** | S |

Separated from E4-5a deliberately: activating a marker-selected job before its
tests exist makes it red under `--min-selected`, and E7-4 then cannot merge past
the job it exists to satisfy.
**Verify:** the job runs E7-4's tests; the wheel-resolution assertion holds.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-7** | Nightly workflow — live jobs only | impl **E8-4**, activate **X-4** | M |

`live-serper`, `live-extraction`, `nightly-summary`. **No mutation job here** —
that ships with its implementation in E12-1, since a job without its configuration
is a scheduled failure.
**Verify:** with the secret removed the job **fails** rather than skipping;
`KINDLY_RUN_LIVE_TESTS=1` is set explicitly in the job env; the summary reports
skip counts; fork PRs never receive the secret.

---

### E5 — L1 component and property tests

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E5-1** | Parser mutual-exclusivity property | impl E0-1 | M |

**Verify:** fails if any parser's matching is widened to overlap another; the
shrunk counterexample is readable.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E5-2** | Per-parser identifier preservation and stable rejection | impl E0-1 | M |
| **E5-3** | Environment resolver tables | impl E0-2 | M |
| **E5-4** | Launch-arg and sandbox resolvers | impl E1-2 | S |
| **E5-5** | Markdown transforms against the corpus | impl E3-3 | M |
| **E5-6** | Text accumulators and encoding-cookie helpers | impl E0-2 | S |

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E5-7** | Redaction helper units — **existing behaviour only** | impl E0-2 | M |

Scoped to what `redact_url_credentials`, `mask_env_values`, `truncate_text` and
`_apply_line_limit` do **today**, extending `test_diagnostics_masking.py` (8 tests,
passing) with property-based cases. It merges green.

Every assertion about the *emit boundary* belongs in E9-1 alongside the sanitizer
that satisfies it. An earlier draft split that red/green pair across two PRs, which
cannot merge under the gate.
**Verify:** properties fail if a masking rule is removed; no test added here fails
on the current tree.

---

### E6 — L2 contract tests

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-1** | MCP tool schema golden | impl E0-2 | M |

Normalized comparison, asserting names, types, required-ness, defaults,
description presence, **and `outputSchema is None`** (subject to X-5).
**Verify:** renaming `num_results` fails it; a description reword does not; passes
on mcp 1.25.0 and the newest allowed release.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-2** | Extract and pin the `KINDLY_DIAG` frame codec | impl E0-2 | M |

**Production change (small).** One encoder/decoder pair, tested both directions
with §4.3's stream edge cases.
**Verify:** each edge case fails if its branch is removed.
`_split_worker_diagnostics` is dead code — flag it for removal separately, do not
target it.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-3** | `page_content` is always a string | impl E0-2 | S |
| **E6-4** | Import/declaration agreement extension | merge **E11-5** | S |

---

### E7 — L3 subsystem tests

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-1** | Server over a real socket | impl E3-4 | M |

One canonical valid `initialize`, one security input varied per case, plus a
no-override control returning 200.
**Verify:** all eight cases; the control case catches a protocol regression that
would otherwise look like a security result.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-2** | `_run_worker_command` parent-side lifecycle | impl E2-3, E3-1, E3-4 | L |

Spawn, stream, heartbeat, termination — the parent's concerns only. Absorbs what
an earlier draft listed separately as a migration of `_fetch_html`'s tests, which
was a misassignment (see E1-3).
**Verify:** clean run returns the child's **HTML** and parsed diagnostics; hanging
child killed at the deadline; killed parent leaves no orphan; stderr garbage does
not crash the parent; non-zero exit surfaces a readable error. Windows and Linux.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-4** | ChromiumPool | impl **E4-5a**, E3-4 | L |

Slot acquisition and release, reuse, pool sizing, port allocation within
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention, concurrency,
shutdown.
**Verify:** after shutdown every spawned PID is gone; the port-range test fails if
the range is ignored. Closes the repo's largest untested module.

---

### E8 — L4 product tests

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-1** | Wheel build, install, import-resolution harness | impl E3-5 | M |

**Verify:** asserts the server module's `__file__` is under the venv's
`site-packages` and **not** the checkout; the assertion fails if run from the repo
root without isolation.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-2** | MCP session over stdio and Streamable HTTP | impl E8-1, E3-2 | L |

**Verify:** `initialize` → `tools/list` → `tools/call` against both entrypoints
with the zero-result fixture; **assert no Chromium process is created**, do not
assume it.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-3** | Deterministic `get_content` case | impl E8-1 | S |

`https://example.invalid/package-smoke.pdf`, plus the L1 guard that every
specialized parser rejects that exact URL.
**Verify:** no network call; the guard fails if a parser is widened —
`parse_arxiv_url` already accepts `arxiv.org/pdf/….pdf`, which is why the host is
pinned.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-4** | Live canaries through the public surface | impl E0-2 | M |

Serper canary via `search_web` or the `web_search` tool — **not** `urllib` as
`test_serper_live.py` does today. Standardize on `KINDLY_RUN_LIVE_TESTS`.
**Verify:** the canary exercises `PROVIDERS` selection (assert the reported
provider); one gate variable across the suite.

---

### E9 — Security

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E9-1** | Sanitize diagnostics at the emit boundary | impl E5-7 | L |

**Production change**, shipped with its tests in one PR. One sanitizing step at
the top of `Diagnostics.emit`, before the entry is appended to `entries`.
**Verify:** both the returned `entries` and the emitted JSON are asserted;
`get_content("https://user:token@host/x")` leaks the token in neither; each §7.1
policy case has a test; a test pins that sanitizing only at the writer would fail,
so nobody relocates it later.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E9-2** | **Decide the outbound request policy** | **X-1** | S |
| **E9-3** | Outbound URL validation tests | impl E9-2 | M |
| **E9-4** | Redirect and DNS-rebinding tests | impl E9-2, E3-4 | M |

---

### E10 — Coverage controls

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-1** | Classification policy test | impl E2-3, E0-4 | M |

Every `src/**/*.py` in the gating scope or in `omit`, exactly once.
**Verify:** a module in neither fails; a module in both fails.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-2** | Non-zero coverage assertion **tool**, unit-tested | impl E10-1 | M |

The checker plus unit tests against **synthetic** `coverage.json` fixtures. It is
not yet pointed at the real tree. This merges green.
**Verify:** given a synthetic report with a zero-covered gating module, the checker
reports failure; given non-zero, it passes.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-3** | Point the assertion at the real tree | merge **E7-2**, **E7-4**, E5-*, E6-* | S |

Separated from E10-2 because switching it on before the L3 tests exist would leave
`main` red, which §1.1 forbids. An earlier draft made "must fail on merge" an
acceptance criterion — that was a deliberate outage.
**Verify:** it passes at merge; reverting E7-4 makes it fail, proving it is live
rather than vacuous.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-4** | `coverage` job and diff-coverage gate | impl E10-1 | M |
| **E10-5** | Baseline bootstrap, ratchet, reset label | impl E10-4 | L |
| **E10-6** | Observational L3 reporting and PR summary | impl E4-4, E4-5b | M |

E10-5 **verify:** bootstrap works with no baseline on the base SHA; a decrease
fails; a decrease with the reset label passes; applying the label re-runs the check
without a manual re-run; an unrecorded rise fails the equality check.

---

### E11 — Async migration

Split into independently mergeable batches. Each converts non-overlapping files,
so several engineers can run in parallel without conflicts.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E11-1** | Convert the search-provider tests (5 files) | merge E1-6, E4-2 | M |
| **E11-2** | Convert the content-loader tests (6 files) | merge E1-6, E4-2 | M |
| **E11-3** | Convert the scrape tests (3 files) | merge E1-6, E4-2, E7-2 | M |
| **E11-4** | Convert the server and resolver tests (4 files) | merge E1-6, E4-2 | M |
| **E11-5** | Verify the migration is complete | merge E11-1…E11-4 | S |

18 `unittest`-style files of 26. Sequenced after the gate is on so a conversion
bug fails visibly rather than blending into stale-test repairs.
**Verify per batch:** each converted test is seen failing before passing (inject a
fault, evidence in the PR); assertion count unchanged or higher.
**E11-5 verify:** no `import unittest` and no `anyio` import remains under
`tests/`; this unblocks E6-4.

---

### E12 — Mutation testing

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E12-1** | Mutation configuration **and** its nightly job | impl E5-1…E5-7, E4-7 | M |

Configuration and job ship together — an earlier draft created the job in E4-7 and
its configuration here, which is a scheduled failure waiting for a second PR.
Scoped to the pure-logic modules (§3.2); Linux-only, since `mutmut` needs `fork()`.
**Verify:** completes within the nightly budget; surviving mutants publish as a
review queue, not a gate.

---

## 5. Dependency graph

Edges that constrain scheduling. `→` impl, `⇢` merge, `⊳` activate.

```
E0-1 → E0-2 ─┬→ E0-3 → E4-1 ⊳ E4-2 (⊳ E1-6, X-2)
             ├→ E0-4 ──────────────┐
             ├→ E2-1 ─┬→ E1-4 ⇢ E2-3 → E2-4
             │        └⊳ E4-3      │
             ├→ E2-2 → E2-3 ──┬────┴→ E10-1 → E10-2 ⇢ E10-3
             │                │                    ↘ E10-4 → E10-5
             │                └→ E7-2 ⊳ E4-4
             ├→ E1-2 → E1-3 ─┐        ↘ E11-3
             ├→ E1-1, E1-5 ──┴⇢ E1-6 ⇢ E11-1,2,4 ⇢ E11-5 ⇢ E6-4
             ├→ E3-1 → E7-2
             ├→ E3-2 → E8-2
             ├→ E3-3 → E5-5
             ├→ E3-4 → E7-1, E7-2, E7-4, E9-4
             ├→ E3-5 → E8-1 ─┬→ E8-2, E8-3
             │               └⊳ E4-6
             ├→ E5-7 → E9-1
             └→ E8-4 → E4-7 (⊳ X-4) → E12-1

X-3 → E4-5a → E7-4 ⊳ E4-5b → E10-6
X-1 → E9-2 → E9-3, E9-4
```

Anything unlisted depends only on E0-2.

**The drawing above is not the source of truth — the per-step `Blocked by` cells
are.** A hand-drawn summary drifts, and the first draft of this plan shipped three
cycles that had to be found by reading. `scripts/check_plan_dag.py` parses the
declarations, rejects references to undefined steps, and fails on any cycle:

```
$ python scripts/check_plan_dag.py
OK: 61 steps, acyclic, longest chain 7
    startable immediately after E0-2: 23
```

Run it on every change to this file; it belongs in the `fast` job once E4-1 exists.

**Cycles removed from the first draft**, all three now verified absent:
E2-3↔E2-1↔E1-4, broken by making E2-1 test-only and adding E2-4; E4-5↔E7-4, broken
by splitting into 5a/5b; E4-7↔E12-1, broken by shipping the mutation configuration
and its job together. E10-2's deliberately-red state is gone with the split into
E10-2 and E10-3.

**23 steps are startable the moment E0-2 lands** — the number that matters for
distributing this across a team, and the reason §2 treats bootstrap as hours
rather than a wave.

---

## 6. Suggested opening

Five engineers.

**Day 1, morning** — one engineer: E0-1 then E0-2. Merge both before lunch.

**Day 1, afternoon — nine streams open.** Suggested assignment:

| Engineer | Starts with | Then |
|---|---|---|
| A | E2-1 → E2-2 | E2-3, E2-4 |
| B | E1-1, E1-2 → E1-3 | E1-5, E1-6 |
| C | E3-1, E3-4 | E7-1, E7-2 |
| D | E0-3 → E4-1 | E0-4 → E10-1, E10-2 |
| E | E5-1, E5-2 | E6-1, E6-3 |

E1-4 slots into engineer B as soon as A's E2-1 lands; E2-3 waits on it. E3-2,
E3-3, E3-5, E5-3, E5-6, E5-7 are unassigned backlog any engineer can pull.

In parallel and off the critical path, the maintainer works X-1 (which blocks
E9-2…E9-4 entirely), X-2 and X-3.

**The single most valuable thing to protect** is that **E2-3** is not deferred: one
refactor unblocks the L3 worker stream, the coverage classification and all of
E10 — and it reads like something that can wait, which is exactly why it slips.
