# Test suite implementation plan

Executable plan for `.system_design/TEST_SUITE.md`. Every step traces to a section
of that document; this file adds sequencing, dependencies and acceptance criteria,
not new design.

**Status:** ready to start. Head at time of writing: `276182c`.

---

## 1. How this plan is sequenced

The ordering optimises for **one thing: opening parallel streams as early as
possible.** Steps that unblock other people come before steps that merely produce
value, even when the latter look more urgent.

That produces a deliberately unbalanced shape:

```
Wave 0   ██                         1 engineer,  ~1 day     — nothing else can start
Wave 1   ████████                   3 engineers, ~3 days    — each step opens a stream
Wave 2   ████████████████████████   8-9 engineers           — the bulk of the work
Wave 3   ██████                     2-3 engineers           — needs Wave 2 largely done
```

**Wave 0 and Wave 1 are the whole game.** They are ~15% of the effort and they
determine whether the remaining 85% can be worked by eight people or two. Resist
the temptation to start writing tests during Wave 1: without the markers, fixtures
and seams, that work lands in a form that has to be redone.

**Why these specific steps unblock:**

| Step | Without it | Streams it opens |
|---|---|---|
| E0-2 pytest config + markers | `--strict-markers` rejects every marker; no job can select | all |
| E0-1 dependencies | no hypothesis, no async plugin, no coverage | all |
| E2-2 `worker_runner.py` split | worker tests must monkeypatch `create_subprocess_exec`; coverage cannot be classified | L3 worker, coverage controls |
| E3-1 fixture child process | no L3 process test can be written | L3 worker |
| E3-2 SearXNG contract server | no cross-process stubbing | L4 |
| E3-3 corpus scaffolding | Markdown transform tests have no fixtures | L1 transforms |
| E2-3 `WorkerProcess` Protocol | doubles drift silently; the 3 loader repairs are blocked | baseline restoration, L3 |

### 1.1 Conventions for every step

- **Atomic.** One reviewable PR. If a step needs two PRs, it is two steps.
- **Self-contained.** It does not require a later step to be useful or correct.
- **Testable.** Every step has an acceptance check that a reviewer can run. Steps
  that add tests are verified by *watching the new test fail first* — per the
  skill's rule 4, a test never seen red is not evidence of anything.
- **Sizes.** S = under half a day. M = half to two days. L = two to five days.
- Steps that change production code state so explicitly; the rest touch only
  `tests/`, CI or config.

---

## 2. Stream map

Once Wave 1 completes, these run independently. Each is a lane one engineer can
own end to end.

| # | Stream | Opens after | Epics | Size |
|---|---|---|---|---|
| 1 | Baseline restoration | E0, E2-3 | E1 | M |
| 2 | L1 parsers & properties | E0 | E5-1…E5-2 | M |
| 3 | L1 resolvers & launch args | E0 | E5-3…E5-4, E5-6 | M |
| 4 | L1 transforms & corpus | E0, E3-3 | E5-5 | M |
| 5 | L2 contracts | E0 | E6 | M |
| 6 | L3 server / socket | E0 | E7-1 | M |
| 7 | L3 worker lifecycle | E0, E2-2, E3-1 | E7-2…E7-3 | L |
| 8 | L3 ChromiumPool | E0, E4-5 | E7-4 | L |
| 9 | L4 product & packaging | E0, E3-2 | E8 | L |
| 10 | Security — diagnostics | E0 | E9-1 | M |
| 11 | CI & coverage controls | E0, E4-1 | E4, E10 | L |

Streams 1 and 11 have a scheduling relationship rather than a code one: E4-2
(branch protection) should not be switched on until stream 1 reaches E1-6.

---

## 3. Epics

### E0 — Foundations

Serial, one engineer, everything waits on it. Do not parallelise; the steps are
small and interdependent.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-1** | Declare test dependencies and the `ratchet` extra | — | S |

Add `pytest-asyncio`, `hypothesis`, `coverage`, `diff-cover`, `mypy` to the `dev`
extra with the bounds in TEST_SUITE §10.2; add a `ratchet` extra; generate
`requirements-ratchet.txt` per §10.4 (clean 3.13 Linux venv, `pip freeze
--exclude-editable`, project filtered out).
**Verify:** `pip install -e .[dev]` succeeds on Windows and Linux;
`requirements-ratchet.txt` contains no local path or VCS URL; a fresh venv
installed from it can run `pytest --version`, `coverage --version`,
`diff-cover --version`, `mypy --version`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-2** | Add `[tool.pytest.ini_options]` with markers | E0-1 | S |

Exactly the block in §10.5: `testpaths`, `--strict-markers`, `asyncio_mode`, and
all seven markers.
**Verify:** `pytest --markers` lists all seven; a test decorated with an
unregistered marker fails collection; the existing suite still runs with the same
pass/fail counts as before the change.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-3** | Add the `--min-selected` collection guard | E0-2 | S |

The `pytest_collection_finish` hook from §10.3 in `tests/conftest.py`.
**Verify:** three measured cases — `-m <unmatched> --min-selected=1` exits 4 with
the count in the message; a run with a genuinely failing test and a satisfied
minimum exits 1; a satisfied minimum with passing tests exits 0. Note
`pytest_collection_modifyitems` is the wrong hook and must not be used.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E0-4** | Add the three coverage configs | E0-1 | S |

`.coveragerc`, `.coveragerc-gate`, `.coveragerc-subprocess` exactly as §10.4
shows.
**Verify:** `coverage run -m pytest …` then `coverage json` produces a report that
**includes `scrape/chromium_pool.py` at zero coverage** — that is the observable
proving `source_pkgs` is doing its job. With `.coveragerc-gate`, that module is
absent from the report instead.

---

### E1 — Restore green

Stream 1. Blocks E4-2 (turning enforcement on) and nothing else.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-1** | Measure and record the Linux baseline | E0-2 | S |

TEST_SUITE §1.1 quotes 11 failures on Windows and predicts 12 on POSIX *from
source, unmeasured*. Run it and record both.
**Verify:** the per-OS numbers are committed into §1.1 replacing the predicted
figure, and the eighth stale caller is confirmed to fail rather than skip on Linux.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-2** | Move sandbox flag/default assertions to L1 | E0-2 | M |

The eight stale `_fetch_html` callers assert two different things (§2.1). Move the
flag and default-resolution half to direct tests of `_build_chromium_launch_args`,
`_resolve_sandbox_enabled`, `_resolve_browser_executable_path`,
`_resolve_start_retry_attempts`, in the style of the passing
`test_worker_launch_args_redaction.py`, with the ambient-state seams of §3.1.
**Verify:** each new test fails when its resolver's default is inverted; the
`--no-sandbox`, root-detection and executable-discovery assertions all survive the
move; no new test calls `_fetch_html`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-3** | Keep retry/cleanup orchestration as narrow `_fetch_html` tests | E0-2 | M |

The other half of E1-2's tests. Autospec doubles around the browser-connect call,
so a signature change fails loudly.
**Verify:** retry-count, terminate-failed-attempt, non-retryable-not-retried and
profile-cleanup assertions all pass; adding a required argument to the patched
callable makes them fail rather than silently pass.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-4** | Repair the three loader tests with a conformant double | E2-3 | M |

They assert command arguments and environment propagation, which a real child
cannot verify (§8A). Rebuild `_FakeProc` as the typed fake from E2-3.
**Verify:** all three pass; deleting `stdout` from the fake makes the conformance
test and mypy fail, which is the regression that started all this.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-5** | Rewrite the concurrency test OS-neutral | E0-2 | S |

Drop the `os.name` patching; keep the explicit-value, malformed, zero, negative
and `num_results`-limiting cases as one parameterized test.
**Verify:** passes on both platforms; each retained case fails if the clamp in
`_resolve_web_search_max_concurrency` is removed.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E1-6** | Suite green on both platforms | E1-1…E1-5 | S |

**Verify:** `pytest` reports **0 failed** on Windows and Linux. This is the gate
for E4-2.

---

### E2 — Production seams

Stream-opening. These change `src/`. §11.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-1** | Extract `_build_worker_command` | E0-2 | S |

Pure function; production calls it. **Production change.**
**Verify:** an L1 test asserts the command shape including `-m
kindly_web_search_mcp_server.scrape.nodriver_worker`; existing loader tests still
pass unchanged.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-2** | Extract `_run_worker_command` into `scrape/worker_runner.py` | E2-1 | M |

**Production change, and the highest-leverage step in the plan.** It opens the L3
worker stream *and* makes the coverage classification expressible (§10.4, §11.2).
`fetch_html_via_nodriver` keeps its signature and always builds its own command;
the command never becomes a public parameter.
**Verify:** `fetch_html_via_nodriver` behaviour is unchanged (the E1-4 tests still
pass); `_run_worker_command` is importable and accepts an arbitrary command;
`grep` finds no `command=` parameter on any public function; `universal_html.py`
retains the Markdown-probe path and no subprocess management.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E2-3** | `WorkerProcess` Protocol, typed fake, mypy config | E0-1 | M |

`@runtime_checkable` Protocol, a concrete fully annotated fake (not `AsyncMock`),
the forcing assignment, `disallow_any_expr` on that surface, and the runtime
contract test (§8A step 3).
**Verify:** the negative fixture — a deliberately `Any`-typed double — **fails**
mypy; removing `stdout` from the fake fails both mypy and the runtime contract
test; `isinstance` alone does *not* catch a wrong `wait()` signature, and a test
documents that so nobody later "simplifies" the static check away.

---

### E3 — Test infrastructure

Stream-opening. Fixtures only; no production code.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-1** | Fixture child process script | E0-2 | M |

A small script that emits known `KINDLY_DIAG` frames on demand, can hang, can exit
non-zero, and can write garbage to stderr.
**Verify:** a smoke test drives it directly for each mode and asserts the observed
behaviour; it starts in under 500 ms on both platforms.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-2** | Local SearXNG-contract HTTP server fixture | E0-2 | M |

Implements the SearXNG response contract, ephemeral port, readiness handshake,
configurable result set including **zero results** (§6.1).
**Verify:** `search_searxng` against it parses results correctly; with
`SEARXNG_BASE_URL` pointed at it and the higher-priority provider variables
cleared, `search_web` selects SearXNG; the zero-result mode returns `[]`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-3** | HTML corpus scaffolding and governance | E0-2 | M |

`tests/corpus/html/`, the `.meta.json` sidecar schema, and a policy test for
§3.3's rules.
**Verify:** the policy test fails on a snapshot with no sidecar, on one over
200 KB, and on one whose sidecar lacks a licence or source URL. Seed with at least
three handcrafted fragments.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-4** | Anti-flake harness helpers | E0-2 | M |

Shared helpers for §5.4: readiness handshake, ephemeral port allocation, isolated
profile directory, PID-tree cleanup keyed on spawned PIDs, child log capture on
failure.
**Verify:** a deliberately hanging fixture child is killed at the deadline and its
PID tree is gone afterwards; cleanup never matches processes by name.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E3-5** | `tests/package/` and its marker policy test | E0-2 | S |

**Verify:** a file added under `tests/package/` without `@pytest.mark.package`
fails the policy test; source-checkout jobs pass `--ignore=tests/package`.

---

### E4 — CI and enforcement

Stream 11. E4-1 and E4-2 are the priority; the rest attach incrementally.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-1** | Workflow skeleton, first job, `ci-required` | E0-3 | M |

The complete trigger list of §10.3, one job selecting
`--ignore=tests/package -m "not live and not chromium and not package"` on Windows
and Linux, and `ci-required` with `if: always()` asserting every dependency is
`success`.
**Verify:** a deliberately failing test makes `ci-required` red; a **skipped**
dependency also makes it red (the failure mode `needs` alone misses); a PR opened
from a fork triggers the workflow.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-2** | Branch protection | E4-1, **E1-6** | S |

`ci-required` as the only required check; require a pull request with no bypass
actors (§10.4's push-to-`main` rule depends on it).
**Verify:** a PR with a red `ci-required` cannot be merged, including by an
administrator.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-3** | `types` job | E2-3 | S |
| **E4-4** | `subsystem` job, Windows + Linux | E7-2 | S |
| **E4-6** | `package` job with the mcp {min,max} axis | E8-1 | S |

Each: added to the workflow and to `ci-required.needs`.
**Verify:** each job's `--min-selected` floor is set and a deliberately typo'd
selector fails it.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-5** | Test-runtime image, and the `chromium` job | E0-1 | L |

Build and publish the image of §10.3 — Python, Chromium, system deps, **no
application code** — from a pinned base digest with pinned package versions.
**Verify:** the digest is recorded in the workflow; the job installs the PR's
wheel at run time and asserts the imported package resolves to it and not to
anything baked into the image.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E4-7** | Nightly workflow | E8-4, E4-5 | M |

`live-serper`, `live-extraction`, `mutation`, and `nightly-summary`. Live jobs set
`KINDLY_RUN_LIVE_TESTS=1` explicitly and assert their secret is present before
collection.
**Verify:** with the secret removed the job **fails** rather than skipping; the
summary reports skip counts; fork PRs never receive the secret.

---

### E5 — L1 component and property tests

Streams 2–4. All blocked only by E0 (and E3-3 for transforms).

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E5-1** | Parser mutual-exclusivity property | E0-1 | M |

Hypothesis: **no URL may be accepted by two parsers** (§3.1).
**Verify:** the property fails if any parser's matching is deliberately widened to
overlap another; a shrunk counterexample is readable.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E5-2** | Per-parser identifier preservation and stable rejection | E0-1 | M |
| **E5-3** | Environment resolver tables | E0-2 | M |
| **E5-4** | Launch-arg and sandbox resolvers | E1-2 | S |
| **E5-5** | Markdown transforms against the corpus | E3-3 | M |
| **E5-6** | Text accumulators and encoding-cookie helpers | E0-2 | S |
| **E5-7** | Diagnostics redaction units | E0-2 | M |

E5-7 is the unit half of E9-1 and can start before the production change lands.
**Verify (each):** table-driven over unset, blank, valid, malformed, zero,
negative and out-of-range where applicable; each case fails if the corresponding
branch is removed.

---

### E6 — L2 contract tests

Stream 5.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-1** | MCP tool schema golden | E0-2 | M |

Normalized comparison (§4.2), asserting parameter names, types, required-ness,
defaults, description presence, **and `outputSchema is None`**.
**Verify:** renaming `num_results` fails it; a description reword does not; it
passes on both mcp 1.25.0 and the newest allowed release.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-2** | Extract and pin the `KINDLY_DIAG` frame codec | E0-2 | M |

**Production change (small).** One encoder/decoder pair used by both sides, tested
in both directions with the stream edge cases of §4.3: fragmentation, several
frames per chunk, CRLF, EOF without newline, multi-byte split across chunks,
malformed payload capping, oversized line truncation.
**Verify:** each edge case fails if the corresponding branch is removed. Note
`_split_worker_diagnostics` is dead code — flag it for removal in a separate PR,
do not target it.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E6-3** | `page_content` is always a string | E0-2 | S |
| **E6-4** | Import/declaration agreement extension | E11-1 | S |

E6-4 waits for the async migration to remove the `anyio` import rather than
declaring the dependency (§4.5).

---

### E7 — L3 subsystem tests

Streams 6–8.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-1** | Server over a real socket | E3-4 | M |

One canonical valid `initialize` request, one security input varied per case, plus
a no-override control case that must return 200 (§5.1).
**Verify:** all eight cases; the control case catches a protocol regression that
would otherwise masquerade as a security result.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-2** | Worker process lifecycle | E2-2, E3-1, E3-4 | L |

**Verify:** clean run returns the child's **HTML** and parsed diagnostics; hanging
child killed at the deadline; killed parent leaves no orphan; stderr garbage does
not crash the parent; non-zero exit surfaces a readable error. Runs on Windows and
Linux.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-3** | Worker retry and cleanup orchestration | E2-2 | M |

Migrates E1-3's assertions onto `_run_worker_command`.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E7-4** | ChromiumPool | E4-5, E3-4 | L |

Slot acquisition and release, reuse, pool sizing, port allocation within
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention, concurrent
acquisition, shutdown.
**Verify:** after shutdown, every PID the test spawned is gone; the port-range
test fails if the range is ignored. This closes the repo's largest untested module.

---

### E8 — L4 product tests

Stream 9.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-1** | Wheel build, install, import-resolution harness | E3-5 | M |

**Verify:** the harness asserts the server module's `__file__` is under the venv's
`site-packages` and **not** under the checkout; the assertion fails if run from
the repo root without isolation.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-2** | MCP session over stdio and Streamable HTTP | E8-1, E3-2 | L |

`initialize` → `tools/list` → `tools/call` against both console entrypoints, with
the zero-result SearXNG fixture so no resolver runs.
**Verify:** no Chromium process is created during the run — assert it, do not
assume it.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-3** | Deterministic `get_content` case | E8-1 | S |

`https://example.invalid/package-smoke.pdf`, plus the companion L1 test asserting
every specialized parser rejects that exact URL.
**Verify:** no network call is made; the guard test fails if a parser is widened
to accept it — `parse_arxiv_url` already accepts `arxiv.org/pdf/….pdf`, which is
why the host is pinned.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E8-4** | Live canaries through the public surface | E0-2 | M |

Serper canary via `search_web` or the `web_search` tool — **not** `urllib` as
`test_serper_live.py` does today — and the thresholded extraction canary.
Standardize on `KINDLY_RUN_LIVE_TESTS`.
**Verify:** the canary exercises `PROVIDERS` selection (assert the chosen provider
is reported); both gates use one variable.

---

### E9 — Security

Stream 10, plus a blocked sub-stream.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E9-1** | Sanitize diagnostics at the emit boundary | E5-7 | L |

**Production change.** One sanitizing step at the top of `Diagnostics.emit`,
before the entry is appended to `entries` (§7.1).
**Verify:** the **returned `entries`** and the emitted JSON are both tested;
`get_content("https://user:token@host/x")` leaks the token in neither; the policy
cases of §7.1 each have a test; a test asserts sanitizing only at the writer would
fail, so nobody moves it later.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E9-2** | **Decide the outbound request policy** | §13.1 — *product decision* | S |

**BLOCKED. This is the only step in the plan that cannot start.** See §5.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E9-3** | Outbound URL validation tests | E9-2 | M |
| **E9-4** | Redirect and DNS-rebinding tests | E9-2, E3-4 | M |

---

### E10 — Coverage controls

Stream 11, after E4-1.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-1** | Classification policy test | E2-2, E0-4 | M |

Every `src/**/*.py` is in the gating scope or in `.coveragerc-gate`'s `omit`,
exactly once.
**Verify:** a new module in neither fails; a module in both fails.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-2** | Non-zero coverage assertions | E10-1 | M |

**Verify:** with `chromium_pool.py` classified L3 and no L3 tests yet, this
**fails** — and passes once E7-4 lands. That transition is the acceptance
criterion; a version that passes today is not implementing the control.

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E10-3** | `coverage` job and the diff-coverage gate | E10-1 | M |
| **E10-4** | Baseline bootstrap, ratchet, reset label | E10-3 | L |
| **E10-5** | Observational L3 reporting and PR summary | E4-4, E4-5 | M |

E10-4 **verify:** bootstrap works with no baseline on the base SHA; a decrease
fails; a decrease with the reset label passes; applying the label re-runs the
check without a manual re-run; an unrecorded rise fails on the equality check.

---

### E11 — Async migration

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E11-1** | Convert the 18 `unittest`-style files to pytest-native async | E1-6, E4-2 | L |

18 of 26 test files. Parallelisable per file across the team once the gate is on —
which is the point of sequencing it here: under an enforced gate a conversion bug
fails visibly instead of blending into stale-test repairs.
**Verify per file:** the converted tests are seen failing before passing (inject a
fault); assertion count is unchanged or higher; `anyio` is no longer imported
anywhere in `tests/` when the last file lands, which is what unblocks E6-4.

---

### E12 — Mutation testing

| ID | Step | Blocked by | Size |
|---|---|---|---|
| **E12-1** | Scoped `mutmut` run in nightly CI | E5-1…E5-7, E4-7 | M |

Scoped to the pure-logic modules only (§3.2). Linux-only — `mutmut` needs
`fork()`.
**Verify:** it runs to completion within the nightly budget; surviving mutants are
published as a review queue, not a gate.

---

## 4. Dependency summary

Only the edges that constrain scheduling:

```
E0-1 ─┬─ E0-2 ── E0-3 ── E4-1 ── E4-2 (also needs E1-6)
      ├─ E0-4 ─────────────────── E10-1
      └─ E2-3 ─┬─ E1-4 ─┐
               └─ E4-3  ├─ E1-6 ── E11-1
      E1-1,3,5 ─────────┤
      E1-2 ─────────────┴─ E5-4

E2-1 ── E2-2 ─┬─ E7-2 (also E3-1, E3-4) ── E4-4
              ├─ E7-3
              └─ E10-1 ── E10-2 ── (passes only after E7-4)
                       └─ E10-3 ── E10-4

E3-2 ── E8-2 (also E8-1)
E3-3 ── E5-5
E3-5 ── E8-1 ─┬─ E8-2
              ├─ E8-3
              └─ E4-6
E4-5 ─┬─ E7-4
      └─ E10-5

E5-7 ── E9-1
§13.1 ── E9-2 ─┬─ E9-3
               └─ E9-4
E11-1 ── E6-4
```

Everything not listed depends only on E0.

---

## 5. Blocked on decisions

| Blocks | Decision needed | Owner |
|---|---|---|
| E9-2, E9-3, E9-4 | **§13.1** — is outbound fetching of private-network addresses intentional? | maintainer |
| — | §13.2 — annotate tools with response models for a real `outputSchema`? Affects E6-1's golden, which currently pins `outputSchema is None` | maintainer |
| E10-5 review cadence | §13.3 — owners for the risk matrix | maintainer |
| — | §13.4 — per-module coverage floors; revisit after E1-1 | maintainer |

§13.1 is the only one that stops work. The rest change the content of a step, not
whether it can start.

---

## 6. Suggested first three weeks

Assuming five engineers. Sizes are relative, not commitments.

**Week 1** — one engineer on E0 (all four steps), then E2-1 and E2-2. A second
picks up E2-3 as soon as E0-1 lands. Nobody else starts; there is genuinely
nothing else that will not need redoing.

**Week 2** — streams open. E3-1, E3-2, E3-3, E3-4, E3-5 in parallel (3 engineers);
E1-1…E1-5 (1 engineer); E4-1 (1 engineer).

**Week 3** — full width. E1-6 and E4-2 land together and the gate goes on. E5, E6,
E7-1 start immediately; E7-2 follows E3-1; E8 follows E3-2.

The single most valuable thing to protect in this schedule is that **E2-2 is not
deferred**. It is one refactor that unblocks the L3 worker stream, the coverage
classification, and E10 — and it is the step most likely to be postponed as
"just a refactor".
