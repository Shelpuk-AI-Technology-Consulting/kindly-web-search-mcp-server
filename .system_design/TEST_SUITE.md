# Test suite design — Kindly Web Search MCP Server

**Status:** To Be. Describes the target test suite, not the one that exists today.

**Measurement environment.** Every number here was measured on Windows 10 Pro
19045 · CPython 3.13 · `mcp` 1.25.0 · `starlette` 0.50.0 · `uvicorn` 0.40.0 ·
`pytest` 9.1.1 · repo at `b3581ca`. Anything derived from source rather than
executed is labelled as such.

---

## 1. Purpose and baseline

The suite has 27 test files (~3,857 lines) against ~6,849 lines of source with no
stated strategy: no `[tool.pytest.ini_options]`, no registered markers, no CI
(`.github/` does not exist), no coverage measurement, and two different
environment gates for live tests.

The reason to write this down is that the suite has a permanently red baseline
everyone has learned to read past.

### 1.1 The baseline, measured

On the environment above: **11 failed, 169 passed, 3 skipped, 9 subtests passed.**

| Tests | Failure | Cause |
|---|---|---|
| 7 of 10 in `test_nodriver_worker_sandbox.py` | `TypeError: _fetch_html() missing 5 required keyword-only arguments: 'reuse_browser', 'remote_host', 'remote_port', 'user_data_dir', 'overall_timeout_seconds'` | `_fetch_html` grew five required arguments; the test callers were never updated |
| 3 of 18 in `test_universal_html_loader.py` | `AttributeError: '_FakeProc' object has no attribute 'stdout'` | `fetch_html_via_nodriver` now streams `proc.stdout`; the fake process never grew one |
| 1 in `test_server.py` | `AssertionError: 3 != 1` | patches `server.os.name` to assert a Windows concurrency cap removed from `_resolve_web_search_max_concurrency` |

**This section is an As-Is record of the defect as it stood at `3af0563`, and
is not maintained as the repairs land.** It describes eight stale callers and
twelve failures because that is what was there; `BASELINE_FAILURES.md` is the
current state, and it is the one with a guard behind it. Read the counts here as
history.

**The count is platform-dependent and Windows reports the smaller number.**
`test_nodriver_worker_sandbox.py` contained **eight** stale `_fetch_html`
callers. The eighth, `test_forces_sandbox_off_when_running_as_root`, skipped on
Windows because `os.geteuid` does not exist there, so POSIX should show **12**
failures. (An earlier draft cited that test's line number; the citation was
struck when E1-2 deleted the test, since a line number outlives the thing it
points at and then points at something else.)

**Measured, and it does.** On Ubuntu 24.04.4 LTS · CPython 3.13.15 · `pytest`
9.1.1, repo at `3af0563`: **12 failed, 303 passed, 2 skipped, 9 subtests
passed** — the same twelve, with the root-detection case that Windows skips. The
passing count differs from the Windows run above only because four bootstrap
steps landed between the two commits and added guard tests; the failing set did
not move. Baselines are quoted per platform, from real runs, or not quoted.

**The counts above are not the artefact that matters.** A count cannot tell a
repaired test from a repaired test alongside a newly broken one, so the failing
**node ids** are recorded per platform in `BASELINE_FAILURES.md` and asserted
against a live child run by `tests/test_baseline_failure_ledger.py`. E1-2 through
E1-5 each delete exactly the ids they repair; E1-6 is reached when the ledger is
empty, which is the same statement as the guard finding no failures.

### 1.2 What is actually uncovered

Most of `scrape/` passes and asserts real behaviour. The uncovered surface is
specific:

- `nodriver_worker._fetch_html` (~480 lines) and the lifecycle it drives —
  `_launch_chromium`, `_wait_for_devtools_ready`, `_terminate_process`, and the
  connect-retry loop. The 8 stale sandbox tests target this.
- `universal_html.fetch_html_via_nodriver` and the parent-side streaming path —
  `_read_stdout_stream`, `_read_stderr_stream`, `_consume_stderr_line`,
  `_emit_worker_heartbeat`, `_terminate_process_tree`. The 3 stale loader tests
  target this.
- **All of `scrape/chromium_pool.py`** (372 lines) — no test file references it.

Working and not to be disturbed: 15 of 18 tests in
`test_universal_html_loader.py` cover the Markdown-suffix probe path end to end,
and `test_worker_launch_args_redaction.py` covers proxy-credential redaction in
`_build_chromium_launch_args` by testing that function directly.

### 1.3 Existing tests worth preserving verbatim

- `test_provider_registry_consistency.py` — holds `PROVIDERS` in sync with the
  README, `.env.example` and the `web_search` docstring. It exists because two
  providers were once added without updating all five copies.
- `test_dependency_constraints.py` — guards the bounds that reach users, since
  the documented `uvx --from git+…` install re-resolves from PyPI and ignores
  `uv.lock`.
- `test_tool_descriptions.py` — keeps the tool docstrings agent-oriented.

---

## 2. Layer model

```
L4  Product      MCP client ─► server ─► provider ─► resolver ─► Chromium ─► Markdown
                 proves:  a real client gets a correct outcome from a real install
                 covers:  MCP session from an installed wheel · live canaries

L3  Subsystem    [server + uvicorn]  [worker + real process]  [pool + real Chromium]
                 proves:  one component and its real infrastructure wire up
                 covers:  transport over a real socket · process lifecycle and
                          cleanup · retry orchestration · ChromiumPool

L2  Contract     MCP tool schema ⇄ clients · parent ⇄ worker frames ·
                 registry ⇄ docs · declared deps ⇄ imports
                 proves:  two parties that change separately still agree

L1  Component    URL parsers · Markdown transforms · env resolvers · launch-arg
                 builders · diagnostics redaction · text accumulators
                 proves:  a unit's logic is right for every input that matters
```

### 2.1 The allocation rule

**Prove each behaviour at the lowest layer that can prove it.**

| Claim | Layer |
|---|---|
| `FASTMCP_ALLOWED_HOSTS=" a , ,b "` parses to `["a","b"]` | L1 |
| `--no-sandbox` is passed when sandbox is disabled | L1 |
| A failed browser connect is retried and the attempt terminated | L3 — orchestration, not resolution |
| Renaming `num_results` breaks every MCP client | L2 |
| A killed worker leaves no orphaned Chromium | L3 |
| A client can install the wheel and call `web_search` | L4 |

**The corollary this codebase got wrong.** Eight sandbox tests assert
worker-internal *flag* decisions by driving `_fetch_html`, an ~480-line coroutine
that launches a browser. Routing a flag assertion through the largest function in
the module is why a five-argument signature change silently disabled all of them.

**The corollary that limits the fix.** Those same eight tests also assert
*orchestration* — that a failed connect is retried and the failed attempt
terminated. That claim genuinely lives in `_fetch_html` and cannot move down.
Splitting them is the work; deleting the orchestration half would trade one gap
for another.

---

## 3. L1 — Component and property tests

### 3.1 Targets

**Launch-argument and sandbox decisions.** `_build_chromium_launch_args`,
`_resolve_sandbox_enabled`, `_resolve_browser_executable_path`,
`_resolve_start_retry_attempts`, `_resolve_snap_backoff_multiplier`,
`_is_snap_browser`, `_is_retryable_browser_connect_error`.

These are **deterministic but not pure**: `_resolve_sandbox_enabled` reads
`os.geteuid()` and `KINDLY_NODRIVER_SANDBOX`;
`_resolve_browser_executable_path` reads four environment variables and then
probes `PATH` via `shutil.which`. Testing them therefore means controlling
ambient state explicitly — `monkeypatch.setenv`/`delenv` for the variables,
`monkeypatch.setattr(shutil, "which", …)` for PATH probing, and
`monkeypatch.setattr(os, "geteuid", …)` for identity, and deleting the attribute
for the platform that has no `os.geteuid`. Pin `shutil.which` even in the cases
that never reach it: an environment-variable case that leaves the real lookup in
place is a case whose result depends on whether the developer has Chromium
installed.

**The `hasattr(os, "geteuid")` guard is not a testable branch.** Deleting the
attribute exercises the *condition*, but the call sits inside a
`try: … except Exception: pass`, so removing the guard makes the bare call raise
`AttributeError`, which is swallowed, and the function returns the identical
answer for every input. Measured. §3.2's mutation runs will report it as a
permanently surviving mutant; it is equivalent, not a hole, and no test should
be written for it.

`test_worker_launch_args_redaction.py` is the working precedent for the style,
and `test_nodriver_worker_launch_resolvers.py` is E1-2's realisation of it —
four resolvers, every ambient input pinned, no browser and no patched
`sys.modules`.

Only **flag and default resolution** moves here. Retry and cleanup orchestration
stays at L3 (§5.2).

**URL parsers.** `parse_stackexchange_url`, `parse_github_issue_url`,
`parse_github_discussion_url`, `parse_wikipedia_url`, `parse_arxiv_url`.

`resolve_page_content_markdown` tries these in order and takes the first that does
not raise, so the load-bearing property is:

> **No URL may be accepted by two parsers.**

An overlap is a silent mis-route with no error anywhere. Hypothesis finds this;
examples do not.

There is **no inverse serializer**, so "round-trip" is not a property this suite
can state. The per-parser properties are instead:

- *Identifier preservation* — for a generated URL built from known identifiers
  (site, question id, owner/repo/number, article title, arXiv id), the parser
  returns exactly those identifiers.
- *Stable rejection* — a rejected URL raises that parser's own error type, never
  a bare `Exception`, and rejection does not depend on trailing slashes, case in
  the scheme, or query-string order.

**Environment resolvers.** The `_resolve_*` families in `server.py`,
`chromium_pool.py` and `nodriver_worker.py`, plus `_parse_port_range`,
`_resolve_transport_security` and `_cors_origin_regex`. Table-driven over unset,
blank, valid, malformed, zero, negative and out-of-range.

**Markdown transforms.** `html_to_markdown`, `sanitize_markdown`,
`extract_content_as_markdown`, `_apply_markdown_cap`, `_build_md_suffix_url`,
against the corpus in §3.3.

**Text accumulators.** `_append_tail_text` and the encoding-cookie helpers in
`nodriver_worker.py`.

### 3.2 Validation by mutation testing

Line coverage proves a line ran, not that a test would fail if it broke. `mutmut`
mutates the source and reports surviving mutants.

Scope: the logic modules above (`utils/diagnostics.py`, the `*_url` parsers, the
`_resolve_*` and launch-arg families). Not `scrape/` plumbing, which L1 does not
own.

**Constraint, confirmed against mutmut's documentation:** mutmut requires `fork()`
support, so on Windows it runs only under WSL. Mutation runs live in Linux CI,
never in the local edit-test loop. Surviving mutants are a review queue, not a
gate.

### 3.3 HTML corpus governance

`tests/corpus/html/` is the input to every extraction test.

- **Two tiers.** Small handcrafted fragments for individual transformations
  (a table, a code block, nested lists, an entity edge case) carry the bulk of
  assertions; a limited set of sanitized real-page snapshots covers
  whole-document behaviour.
- **Provenance and licensing.** Each snapshot has a sidecar `.meta.json` naming
  source URL, capture date, and licence or rationale. Do not snapshot pages whose
  terms forbid redistribution.
- **Sanitization before commit.** Strip cookies, tokens, session identifiers,
  personal data, analytics and third-party script bodies. A committed snapshot is
  published.
- **Size cap.** 200 KB per snapshot, trimmed to the region under test.
- **Assertion style.** Structural by default (headings preserved, code fences
  intact, no raw HTML left, length within cap). Golden-file matching only for the
  small handcrafted fragments, where a diff is readable.
- **When snapshots are added or refreshed.** Three triggers, all reviewed commits
  stating what changed: a reported extraction regression (add the page that
  broke); a deliberate extractor change (update the affected expectations in the
  same PR); a live canary failure showing a real page changed shape. Never an
  automatic overwrite.

---

## 4. L2 — Contract tests

### 4.1 Keep as-is

The three in §1.3.

### 4.2 The MCP tool surface

Three distinct things are often conflated here. They need separate tests because
only the first is what clients actually receive.

**(a) The generated tool schema — what clients bind to.** Reachable via
`await mcp.list_tools()`:

```
web_search  -> properties {'num_results', 'query'}, required ['query']
get_content -> properties {'url'},                  required ['url']
```

**`outputSchema` is `null` for both tools** (verified). Both are annotated
`-> dict`, so FastMCP derives no result schema and clients receive none. A test
asserting `WebSearchResponse` structure from `list_tools()` would assert
something that does not exist.

Assert here: parameter names, JSON types, required-ness, defaults, descriptions
present (not their exact text), and — as a deliberate, reviewed fact —
`outputSchema is None`. That last assertion is what will fail the day someone
changes the annotations, which is the point.

**(b) The Pydantic models — an internal shape.** `WebSearchResponse`,
`GetContentResponse` and `WebSearchResult` get their own `model_json_schema()`
tests. These are not exposed to clients today; testing them separately keeps that
distinction visible.

**(c) Runtime result validation.** Call each tool and validate the returned
`dict` against the corresponding model. This is what actually ties (a) and (b)
together at present.

**Comparison hygiene for (a).** Normalize before comparing — sort keys, drop
generated `title` fields, compare descriptions by presence — because the allowed
SDK range (`mcp>=1.25,<2`) may reorder keys or rewrite generated text in a minor
release. Run against the minimum (`1.25.0`) and the newest allowed release
(§10.3).

**Note for the owner:** annotating the tools with their response models would
give clients a real `outputSchema`. That is a production API change with client
impact, not a test change, and is listed in §13.

### 4.3 The parent ⇄ worker frame format

`nodriver_worker.py` reports to `universal_html.py` by framing lines on stderr:

```
KINDLY_DIAG {"stage": "...", "msg": "...", ...}
```

**Test the live path.** `_split_worker_diagnostics` (`universal_html.py:123`)
parses a whole captured stderr string and **has no callers in `src/` or
`tests/`**; production streams through `_read_stderr_stream` →
`_consume_stderr_line`. That dead function is a removal candidate, flagged here
rather than deleted by this document.

Both sides ship from the same wheel, so this is an **internal protocol**, not an
agreement between independently versioned parties. It still earns a contract test
because the two sides are edited independently and the format is all that holds
them together.

Extract the frame encoder and decoder into one place and test both directions:

- Emission through the real encoder produces a line the real decoder accepts.
- Streaming consumption handles fragmentation: a frame split across chunk
  boundaries, several frames in one chunk, CRLF endings, EOF with no trailing
  newline, and a multi-byte character split across chunks.
- Malformed payloads are sampled and capped without raising; non-`KINDLY_DIAG`
  lines survive as human-readable stderr.
- An oversized line is truncated rather than buffered without limit.

### 4.4 Response-model invariants

`page_content` is promised **always a string** by the tool docstring and
`models.py`, on every failure path. `resolve_page_content_markdown` can return
`None` and both tools convert it. Assert across each branch: timeout, handler
exception, unsupported type, empty provider result.

### 4.5 Import/declaration agreement

`tests/test_searxng_unit.py` imports `anyio`, declared nowhere. The fix is to
remove the import during the async migration (§10.1), not to declare the
dependency. The guard should then assert test-only imports stay within the
declared `dev` extra.

---

## 5. L3 — Subsystem tests

Split by the infrastructure actually required, because that determines where they
can run.

### 5.1 Server over a real socket — portable

Start the server on an ephemeral port and drive it with `httpx`. No browser, so
this runs on Windows and Linux.

**Define one canonical valid request and vary exactly one security input per
case.** A bare `POST /mcp` is not a valid MCP request: Streamable HTTP requires
`Content-Type: application/json`, `Accept: application/json, text/event-stream`
and a JSON-RPC body, and a protocol-level `400`/`406` is easily mistaken for the
expected `403`/`421`. The fixture is a single well-formed `initialize` request;
each case overrides one header.

| Configuration | Varied input | Expected |
|---|---|---|
| default | `Origin: https://evil.example` | `403` |
| default | non-loopback `Host` | `421` |
| default | preflight, foreign origin | no `Access-Control-Allow-Origin` |
| default | preflight + `POST`, `http://localhost:3000` | `200`, origin echoed |
| `FASTMCP_ALLOWED_HOSTS` set | `Host` = service name | `200` |
| `FASTMCP_ALLOWED_HOSTS` set | loopback origin | `200` |
| `FASTMCP_ALLOWED_HOSTS` set | foreign origin | `403` |
| `--sse` | `GET /sse` / `GET /mcp` | stream held open / `404` |

A control case with no overrides must return `200`, so a protocol regression
fails visibly rather than masquerading as a security result.

### 5.2 Worker process lifecycle and orchestration — portable

Two groups, both needing the seam in §11.2, neither needing Chromium.

**Lifecycle**, against a real fixture child that emits known frames on demand and
can be told to hang: a clean run returns the child's **HTML** output
(`fetch_html_via_nodriver` returns rendered HTML — Markdown conversion happens
later in `load_url_as_markdown`) plus parsed diagnostics; a hanging child is
killed at the deadline; a killed parent leaves no orphaned child; garbage on
stderr does not crash the parent; a non-zero exit surfaces a readable error.

**Orchestration**, preserved from the stale sandbox tests: a retryable connect
error is retried up to the configured attempts; each failed attempt is
terminated before the next; a non-retryable error is not retried; the profile
directory is cleaned up with `ignore_cleanup_errors`; and a missing browser
executable surfaces the install and `KINDLY_BROWSER_EXECUTABLE_PATH` guidance
rather than an error from Chromium. That last one is here rather than at L1
because the resolver's contribution is only `None` — the translation into
actionable advice is `_fetch_html`'s, and it is the sole guidance a user gets
when no browser is installed. These use narrow doubles around `_fetch_html`,
built from the real interface (autospec) so a signature change fails loudly.

**Autospec for callables, a Protocol for the process object.** Autospec validates
call signatures, which is exactly what the eight stale tests needed and did not
have. It does *not* declare instance attributes: `create_autospec` on
`asyncio.subprocess.Process` gives no `stdout`, `stderr`, `stdin` or `pid`
(§8A step 3), so the process double is pinned by a Protocol instead. Use each for
the failure it actually catches.

**The Protocol half applies to the lifecycle group, not the orchestration one.**
Only the lifecycle tests *consume* a process — they read its streams and its
exit status. The orchestration tests pass it through: `_fetch_html` holds the
object and hands it to `_terminate_process` and `_wait_for_devtools_ready`,
both of which those tests double, so nothing reads an attribute off it. E1-3
therefore uses a plain identifiable stub and E2-1 owns the typed definition, in
`scrape/types.py`, in production. That ordering is deliberate: a Protocol
declared in a test module now would be the second definition of a shape E2-1
exists to give exactly one, which is the drift it is there to prevent.

These complement the L1 flag tests of §3.1 — a fixture child cannot assert which
Chromium flags were chosen, and a resolver test cannot assert a process tree
died.

**Open: which job runs the orchestration group.** §9 routes "Worker
retry/termination orchestration" to the `subsystem` job, but §10.5 registers
that marker as *"needs a real socket or child process"* — and orchestration
tests written correctly need neither. E1-3 built them with every collaborator
doubled: no socket, no child, ~1 s for the module. Left **unmarked** they fall
into the `fast` job instead.

That is not only a routing question. `.coveragerc-gate` omits
`scrape/nodriver_worker.py`, and §10.4's control 1 requires every omitted module
to show non-zero coverage in the **observational L3 report**, which the
`subsystem` and `chromium` jobs produce and `fast` does not. So an unmarked
orchestration group leaves that module's only evidence of being exercised in a
report nothing collects.

Three ways out, for the CI workstream to choose between rather than for a
repair step to settle by hand: widen the `subsystem` description to cover
"drives a subsystem's orchestration, with or without real infrastructure"; add a
distinct marker; or have control 1 read the `fast` report too. E1-3 deliberately
changed nothing here — the marker list is a contract guarded by
`tests/test_pytest_configuration.py` against this document, the control that
depends on it does not exist yet, and widening a registered meaning on
speculation is the wrong direction. Resolve it with E4-1, whose job definitions
force the answer anyway.

### 5.3 Chromium-specific — Linux container

`ChromiumPool`: slot acquisition and release, reuse
(`KINDLY_NODRIVER_REUSE_BROWSER`), pool sizing
(`KINDLY_NODRIVER_BROWSER_POOL_SIZE`), port allocation within
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention
(`KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS`), concurrent acquisition, shutdown.
Plus one real fetch through `_fetch_html` against a locally served page.

### 5.4 Anti-flake and cleanup requirements

Every test in §5.2 and §5.3 that starts something real must:

- Use a **readiness handshake**, never a sleep — wait for a known line or an
  accepting port, with a timeout.
- Allocate **ephemeral ports** and **isolated profile directories** per test.
- Carry a **per-test timeout** shorter than the job timeout.
- Clean up in `finally`, keyed on **the PIDs this test spawned**. "No live
  browsers" means those children and their descendants are gone — never a scan
  for processes named `chrome`, which is vulnerable to PID reuse and would kill a
  developer's own browser.
- Capture and attach child stdout/stderr on failure.

**Four of those five do not bind a fully-doubled test**, and saying so is not a
loophole — it is what stops the list reading as ignored. A test that starts no
process has no endpoint to hand-shake with, no port to allocate, no pid to reap
and no child output to capture. The **per-test timeout does still bind**, and
harder than elsewhere: the orchestration group drives a retry loop containing
sleeps, so an unbounded call hangs the suite instead of failing.

A wall-clock bound is necessary there and not sufficient. Measured in E1-3: with
the loop mutated to spin, a 10 s bound fired and the test still took 47 s to
report, because mock doubles record every call and a zero-backoff loop records
millions. Bound the *work* as well as the clock — cap the number of attempts a
double will serve, and fail with a sentence naming the loop.

---

## 6. L4 — Product tests

### 6.1 Deterministic MCP session — every PR

Build the wheel, install it, and drive the real console entrypoints
(`kindly-web-search-mcp-server start-mcp-server`, `mcp-web-search --http`) with
the SDK's own client: `initialize` → `tools/list` → `tools/call`. Being
deterministic it belongs in the per-PR gate, and running from an installed wheel
covers packaging and entrypoints, which nothing else touches.

**Stubbing across a process boundary.** The server runs in a separate process, so
`monkeypatch` cannot reach it and there is no provider-injection API. The
mechanism is **configuration, not patching**: stand up a local HTTP server that
implements the SearXNG response contract, and start the child with
`SEARXNG_BASE_URL` pointing at it and `SERPER_API_KEY`, `SERPBASE_API_KEY` and
`TAVILY_API_KEY` cleared, so SearXNG wins provider selection. `search_searxng` is
configured entirely by URL, which makes it the natural seam.

**Do not add a production "test provider" hook.** An unrestricted injection point
in shipped code is a larger risk than the test is worth, on a server that is
already unauthenticated.

**Controlling the resolver, which the provider stub does not.** Stubbing search is
not sufficient for determinism. On a non-empty result set `web_search` calls
`resolve_page_content_markdown` for **every** returned link, which falls through
to the universal loader and launches Chromium. The `package` job has no browser,
so an unconstrained fixture response would either hang or fail on infrastructure.

Two tool calls, each deterministic by construction:

1. **`web_search` against a fixture that returns zero results.** `search_web`
   returns `[]`, `web_search` short-circuits before the enrichment fan-out, and no
   resolver runs. This exercises provider selection through `PROVIDERS`, the tool
   wrapper, the transport and the installed entrypoint. It is explicitly a
   **routing, protocol and packaging smoke test** — it does not touch content
   resolution, and the document does not claim otherwise.
2. **`get_content` on `https://example.invalid/package-smoke.pdf`.**
   `load_url_as_markdown` calls `_is_probably_pdf_url` as its **first** statement
   and returns `None` before any probe or browser launch, so the tool returns its
   deterministic "Could not retrieve content" note with no network and no
   Chromium. This exercises the second tool end to end, including the `None` →
   Markdown-note conversion that §4.4 pins.

   **The host matters, not just the `.pdf` suffix.** `get_content` runs the
   specialized resolver chain first, and that chain is matched on URL shape, not
   on extension. Verified: `parse_arxiv_url("https://arxiv.org/pdf/2401.12345.pdf")`
   **accepts** and returns `2401.12345`, so an arXiv PDF routes to the arXiv
   handler and makes network calls long before `load_url_as_markdown` is reached.
   The fixture URL must therefore be on a host no specialized parser claims —
   `example.invalid` is reserved by RFC 2606 and cannot resolve even if something
   tried. A companion L1 test asserts every specialized parser rejects the exact
   fixture URL, so the smoke test cannot silently start hitting the network if a
   parser's matching widens later.

Anything requiring real content resolution is a `chromium` or live job, not this
one.

**Isolation and cleanup.** This job starts two real entrypoints and a local
fixture server, so §5.4's anti-flake rules apply here in full, plus two
requirements specific to testing an installed artefact:

- **A fresh virtual environment**, and a working directory **outside the
  checkout** — necessary but **not sufficient**. `tests/conftest.py` inserts
  `<checkout>/src` at `sys.path[0]` unconditionally, so any invocation that loads
  it shadows the installed package from every working directory, and the cwd is
  not the mechanism. Measured; see §10.3, where the unresolved conflict and its
  two candidate resolutions are recorded.
- **Assert the import actually resolves to the wheel.** The test asserts the
  server module's `__file__` lies under the venv's `site-packages` and not under
  the checkout. Without it the job can pass while exercising the source tree — a
  false positive on the one thing this job exists to prove.
- Ephemeral port and readiness handshake for the fixture server; never a sleep.
- Per-call and whole-process timeouts, so a failed entrypoint fails fast instead
  of hanging until the job timeout.
- `finally` cleanup keyed on the PIDs this test spawned, with child stdout and
  stderr captured and attached on failure.

### 6.2 Live canaries — nightly, gated

**Provider canary.** One real query per credentialed provider, asserting shape and
non-emptiness, never exact content. It must run through the **public routing
surface** — at minimum `search_web`, preferably the `web_search` tool — so it
exercises `PROVIDERS` selection and the tool wrapper. The existing
`test_serper_live.py` calls Serper directly with `urllib.request` and therefore
proves only that the vendor is up; it does not cover routing and must be
rewritten or supplemented.

**Extraction canary.** `get_content` against a small fixed URL list, thresholded:
non-empty, above a minimum length, contains an expected anchor phrase, is not the
deterministic failure note. Real pages change, so equality assertions here
generate flakes. A failure is the trigger to refresh the corpus (§3.3).

### 6.3 Gating

**One gate.** Live tests are split today across `KINDLY_RUN_LIVE_TESTS`
(`test_live_fetch_urls.py`) and `RUN_LIVE_TESTS` (`test_serper_live.py`).
Standardize on **`KINDLY_RUN_LIVE_TESTS=1`** plus `@pytest.mark.live`. Every
nightly job must set it explicitly in its `env:` block; selecting the marker
alone leaves the tests skipping.

**A skipped live suite is a failed live suite.** One CI job per credentialed
provider or capability; each **enabled** job asserts its required secret is
present before collection and fails if it is missing; the run reports skip
counts; a named owner is alerted on scheduled-workflow failure.

**Credentials available today.** `SERPER_API_KEY` exists as a repository Actions
secret, enabling two jobs now:

- **`live-serper`** — Serper is first in `PROVIDERS`, so this canary covers the
  routing path most installs take.
- **`live-extraction`** — needs a browser but **no provider credential**, because
  `get_content` fetches a URL without going through search.

SerpBase, Tavily, SearXNG and Sofya stay **out of the matrix** until credentials
exist, rather than sitting in it skipping green. Adding a secret is what adds the
job.

---

## 7. Security testing

### 7.1 Diagnostics must be sanitized at the boundary

`Diagnostics.emit` and `emit_diagnostic` (`utils/diagnostics.py:133,151`) apply
**no redaction** — only JSON serialization and a line-length cap. Callers pass
raw data: `server.py` emits `{"url": url}` and `{"detail": full_detail}` where
the detail is unfiltered exception text. So
`get_content("https://user:token@host/x")` places the credential verbatim in
diagnostics.

This is an **egress** path, not merely logging: `Diagnostics.entries` is returned
to the caller in `GetContentResponse.diagnostics` and
`WebSearchResult.diagnostics`.

**Sanitize before the entry is appended to `entries`, not merely before the
stderr write.** `emit` appends to `self.entries` and then calls
`emit_diagnostic`; sanitizing only inside the writer would clean stderr while
leaving the raw value in the MCP response — the worse of the two paths. One
sanitizing step at the top of `emit`, covering both consumers, is the
requirement.

Test the **emitted JSON and the returned `entries`**, not the helper. Policy must
state, and tests must cover:

- URL userinfo (`user:pass@`), raw and percent-encoded.
- Credential-bearing query parameters (`?token=`, `?api_key=`, `?sig=`) — the
  name list, and the behaviour for an unrecognized name.
- Request and response headers: `Authorization`, `Cookie`, `Set-Cookie`.
- Exception messages and tracebacks, which routinely embed the failing URL.
- Page-content samples (`sample_data`), which can contain anything.
- Low-entropy secret values, where substring matching yields false positives —
  the policy must say whether it redacts by key name, by pattern, or both.

### 7.2 Outbound request policy

**Decided 2026-08-31.** The server is unauthenticated and `get_content` fetches
whatever URL a caller supplies, so the policy is stated here rather than left to
implementation.

**Scheme.** `http` and `https` only. `file:`, `data:`, `ftp:` and `chrome:` are
refused. They are reachable today by accident — no loader handles them — and each
has its own failure mode.

**Address, by provenance.** The two are different code paths and hold different
policies:

| Provenance | Policy |
|---|---|
| **Externally supplied** — `get_content`'s `url`, and the links `web_search` fans out to | **Public addresses only.** Loopback, RFC1918, link-local, IPv6 unique-local, cloud metadata (`169.254.169.254`) and their IPv4-mapped IPv6 forms are refused. |
| **Operator-configured** — `SEARXNG_BASE_URL`, `KINDLY_CHROME_PROXY` | **Unrestricted.** The operator chose these; reaching a private address here is the intended behaviour and nothing an attacker influences. |

Restricting the externally-supplied path does **not** break a self-hosted SearXNG
deployment, because that runs through `search_searxng` against an
operator-configured URL rather than through the resolver.

**A hostname resolving to several addresses** must have *every* returned record
permitted. Pinning to one validated address would be stronger, but the connectors
do not expose that control; requiring all records is enforceable with what exists.

**Enforcement mechanism, by stack.** These differ because the stacks differ:

- **`httpx`** — enforced. Resolution happens in Python, so the address is
  observable and the check runs at validation, at every redirect hop, and against
  the resolved address set. E3-6's seam is what makes this possible.
- **Chromium** — **pre-navigation only**. The scheme and the resolved address of
  the URL handed to the browser are checked before navigation. Chromium then
  resolves and connects inside its own process, so two cases are **not** enforced:

  1. **DNS rebinding** — the browser re-resolves, and may reach an address the
     check never saw.
  2. **Subresources** — a permitted public page can request a private address
     through an image, script, frame, stylesheet or `fetch`, and the server never
     sees that URL.

**These two gaps are accepted, not overlooked.** Closing them needs a local
policy-enforcing proxy that mediates every browser request — a component the
project would own and maintain. That was judged disproportionate for a server
whose realistic exposure is a caller naming a private address directly, which
pre-navigation checking does stop. §7.3 records the trigger for revisiting.

**Upstream proxy — a trusted boundary.** With `KINDLY_CHROME_PROXY` set to an HTTP
`CONNECT` proxy, the **proxy** resolves the hostname and the server never sees the
destination address. Address policy therefore cannot be enforced through it. The
proxy is operator-configured, so this is where the guarantee stops rather than an
attacker-controlled path, and it is documented as such.

**Tests at five boundaries.** Enforcement is now settled, but the tests are not
conditional on it — the two accepted gaps need characterizing precisely because
they are gaps:

1. **URL validation** — L1, table-driven over scheme, address class, and
   multi-record answers including mixed public/private and dual-family results.
2. **Redirect handling** — L3, a local server issuing a public→private redirect;
   enforced on the `httpx` path.
3. **Connection** — L3, a hostname whose resolution changes between validation and
   connect; **enforced** for `httpx`, **characterized as a known gap** for
   Chromium.
4. **Browser-initiated requests** — L3, a permitted public page attempting a
   private subresource. **Characterized as a known gap**: the test records that the
   request succeeds today, so that if a policy proxy is ever added the test flips
   and someone notices.
5. **Upstream proxy** — L3, a request whose hostname a configured `CONNECT` proxy
   resolves. **Characterized**, pinning where the guarantee ends.

A characterization test asserting a gap is not an endorsement of it. It is what
stops the gap being rediscovered, and what makes closing it observable.

### 7.3 Revisiting the accepted gaps

The Chromium subresource and rebinding gaps are accepted on the basis that this
server runs where its operator can see it. **Revisit if any of these becomes
true:** the server is exposed beyond loopback or a trusted network; it gains
authentication and therefore multi-tenant use; or a prompt-injection path is found
that reliably steers `get_content` at attacker-chosen pages. Any of those changes
the exposure that justified the trade, and the answer is the local
policy-enforcing proxy of §13.1's candidate list.

---

## 8. Workstreams, in order

**A. Restore green.** Blocks everything else, and is done with *minimal* edits —
no framework migration mixed in, so a failure means a stale test, not a
conversion bug.

1. Measure the baseline on Linux; record both platforms — as node ids in
   `BASELINE_FAILURES.md`, not only as counts, and guarded against a live child
   run by `tests/test_baseline_failure_ledger.py`. Steps 2-4 below each delete
   exactly the ids they repair, so the ledger drains to empty as they land.
2. Split the 8 stale sandbox tests: flag and default resolution moves to L1
   (§3.1); retry and cleanup orchestration stays as narrow autospecced tests
   around `_fetch_html` (§5.2).
3. Repair the 3 stale loader tests. They assert command arguments and environment
   propagation, which a real child cannot verify, so a double is required.

   **Autospec does not close this gap — verified.**
   `create_autospec(asyncio.subprocess.Process, instance=True)` declares
   `returncode`, `wait`, `kill` and `terminate`, but **not** `stdout`, `stderr`,
   `stdin` or `pid`: those are set on the instance in `__init__`, so a
   class-based spec never sees them. An autospec double would therefore have
   accepted the very `_FakeProc` that was missing `stdout`.

   Define instead a `WorkerProcess` **Protocol** naming the exact surface
   production consumes — `stdout`, `stderr`, `pid`, `returncode`, `wait()`,
   `kill()`, `terminate()` — annotate `_run_worker_command` with it, and have the
   fake implement it.

   **Three mechanisms are needed, because none of them is sufficient alone.**
   Measured behaviour of `isinstance` against a Protocol:

   | Check | Missing `stdout` | Wrong `wait()` signature |
   |---|---|---|
   | `isinstance` without `@runtime_checkable` | `TypeError` | `TypeError` |
   | `isinstance` with `@runtime_checkable` | caught (`False`) | **not caught** (`True`) |
   | static type check | caught | caught |

   So:

   1. Mark the Protocol `@runtime_checkable` — without it `isinstance` raises
      `TypeError` rather than answering. Use it for the **presence** check only.
   2. A **static type-check job** (§10.3) is what catches signature drift, which
      is the other half of the original outage: `_fetch_html` gaining five
      arguments is exactly the failure a runtime `isinstance` returns `True` for.

      **Declaring a Protocol and a fake does not make mypy compare them.** The
      conformance must be forced by an assignment the checker has to verify:

      ```python
      # tests/doubles/worker_process.py
      _contract: WorkerProcess = FakeWorkerProcess()   # mypy checks this line
      ```

      Two conditions, or the check passes vacuously. The fake is a **concrete,
      fully annotated class** — not `AsyncMock`, whose members infer as `Any` and
      satisfy any Protocol at all. And the job enables **`disallow_any_expr`** on
      this surface: `disallow_any_explicit` rejects only written-out `Any`
      annotations and `disallow_untyped_defs` only unannotated definitions, so
      neither catches an `Any` that arrives by inference.

      A negative fixture proves the job is not vacuous: a deliberately
      `Any`-typed double that mypy **must** reject, asserted in CI. A type-check
      job that cannot fail is indistinguishable from no job.
   3. An explicit **runtime contract test** asserting each attribute exists, each
      method is callable, and each async method returns a coroutine — because
      `runtime_checkable` verifies presence only, not types or arity.

   Keep the real-child contract test of §5.2 as well: the Protocol pins the shape,
   the static check pins the signatures, the real child pins the behaviour. None
   substitutes for another.
4. Rewrite the concurrency test OS-neutral. Only the Windows default is obsolete;
   its explicit-value, malformed, zero and negative cases are real requirements
   with no other home. One parameterized test over defaulting, validation,
   clamping and `num_results` limiting, with the `os.name` patching removed.

**B. Enforce green — immediately after A, before anything else.** The failure this
document exists to fix was not that tests broke; it was that a red suite was
never enforced and became normal. Deferring enforcement until the rest of the
plan is built would let exactly that recur while B–D are in flight.

The minimum viable gate is small and ships as soon as A lands:

1. One workflow running
   **`pytest --ignore=tests/package -m "not live and not chromium and not package"`**
   on Linux and Windows, Python 3.13. The `--ignore` is not redundant with the
   marker: §10.3 requires it on every source-checkout job precisely because the
   marker is the thing that gets forgotten.
2. The `ci-required` aggregation job (§10.3) as the **required check** under
   branch protection on `main`.

**The initial selection must include `subsystem`, not just `fast`.** Workstream A
restores the worker retry, cleanup and streaming tests, and §5.2 places them at
the subsystem layer — they need a real child process. A `fast`-only gate excludes
`subsystem` by construction, so `ci-required` would go green while the exact
tests this document exists because of sat unprotected. The initial selection
therefore excludes only what the first runner genuinely cannot do: Chromium and
the network. Those tests are portable by design (§5.2), so both platforms can run
them from day one.

Every later job — `fast-extras`, `chromium`, `package` — is added to the same
workflow and to `ci-required`'s `needs` as its tests come into existence, and the
broad selection is split into the named jobs of §10.3 at that point. The
nightlies are **not** added to `ci-required` (§10.3).

**C. Add the production seams** in §11 — the rest of the design depends on them.

**D. New coverage,** in risk order (§9): security (§7), then the untested worker
lifecycle and `ChromiumPool`, then contracts, then providers and loaders. Each
increment adds its job to `ci-required`.

**E. Async migration.** Convert `IsolatedAsyncioTestCase` and `anyio.run`
wrappers to pytest-native async, file by file. Deliberately last: it is a broad
mechanical change, and running it under an already-enforced gate means a
conversion bug fails visibly instead of blending into stale-test repairs.

---

## 9. Risk-to-test matrix

The **Today** column describes *test coverage*, not implementation status.
**Owner** is intentionally blank (§13).

| Subsystem / behaviour | Today | Target layer | CI job | Owner |
|---|---|---|---|---|
| Provider routing, strict order, no fallback | covered | L1 | `fast` | |
| Serper / SerpBase / Tavily / SearXNG / Sofya parsing | partial | L1 | `fast` | |
| Provider errors: 401, 429, malformed JSON, timeout, empty | gap | L1 (`httpx.MockTransport`) | `fast` | |
| Provider registry ⇄ docs | covered | L2 | `fast` | |
| StackExchange / GitHub issues / GitHub discussions / Wikipedia | partial — parsing covered, failure paths thin | L1 + L2 | `fast` | |
| arXiv + PDF extraction | partial | L1 | `fast` | |
| `pdf-advanced` extras present *and* absent | gap | L2 | `fast` (both installs) | |
| Resolver routing and per-handler fallback | partial | L1 | `fast` | |
| Markdown transforms | partial | L1 + corpus | `fast` | |
| URL parser mutual exclusivity | gap | L1 property | `fast` | |
| Env resolvers across all three modules | partial | L1 | `fast` | |
| Diagnostics redaction at the emit boundary | gap (§7.1) | L1 + L2 | `fast` | |
| Outbound URL policy | gap, undefined (§7.2) | L1 + L3 | `fast` + `subsystem` | |
| Inbound transport security, CORS, SSE | implemented in PR #50; **automated L3 gap** | L1 + L3 | `fast` + `subsystem` | |
| MCP tool schema stability | gap | L2 | `fast` | |
| Parent ⇄ worker frame format | gap | L2 | `fast` | |
| Worker lifecycle and cleanup | gap — stale | L3 portable | `subsystem` | |
| Worker retry/termination orchestration | covered — unmarked, see §5.2 | L3 portable | `subsystem` (open) | |
| ChromiumPool | gap — no tests | L3 Chromium | `chromium` | |
| CLI entrypoints and `--` forwarding | covered | L1 | `fast` | |
| Wheel build, install, console entrypoints | gap | L4 | `package` | |
| Documented `uvx --from git+…` path | gap | L4 | nightly | |
| Dependency bounds | covered | L2 | `fast` | |
| Tool-call cancellation and partial results | gap | L3 | `subsystem` | |
| Output size limits and truncation | partial | L1 | `fast` | |
| Live Serper reachability via `search_web` | gap — existing test bypasses routing | L4 | `live-serper` | |
| Live SerpBase / Tavily / SearXNG / Sofya | not enabled — no credential | L4 | — | |
| Live extraction quality | gated | L4 | `live-extraction` | |

---

## 10. Tooling, configuration and CI

### 10.1 Async direction

The suite mixes `unittest.IsolatedAsyncioTestCase` with hand-rolled `anyio.run`
wrappers. **Standardize on `pytest-asyncio` with `asyncio_mode = "auto"`** — the
project is pure asyncio, so the marker is noise. Migration removes every
`anyio.run` wrapper, which is what lets `test_searxng_unit.py` stop importing
`anyio` (§4.5). Sequenced as workstream D, not during baseline restoration.

### 10.2 Dependencies and version policy

| Tool | Constraint | Purpose |
|---|---|---|
| `pytest` | `>=9,<10` | Runner |
| `pytest-asyncio` | `>=1.4,<2` | Async tests, `asyncio_mode = "auto"` |
| `hypothesis` | `>=6.167,<7` | Properties in §3.1 |
| `coverage` | `==7.13.5` in the pinned lane, `>=7.10.3,<8` elsewhere | Branch coverage and the committed baseline. 7.10.0 introduced `patch = subprocess`; **7.10.3** fixed missed nested children, data stranded when a child changes directory, Windows startup failures and incomplete config propagation — all of which the worker tests hit directly (§10.4) |
| `diff-cover` | `>=10.4,<11` | Diff coverage on changed lines; 10.4 is the floor because `--branch-coverage` does not exist below it (§10.4) |
| `mutmut` | `>=3,<4` | L1 validation; **needs `fork()` — Linux/WSL only** |
| `ruff` | `>=0.6,<1` | Lint |
| `mypy` | `>=2.3.1,<3` | Static check for the test-double Protocols (§8A step 3) |
| `packaging` | `>=24` | Dependency guard |

Bounds, not "current": this project has twice been broken by an unbounded
dependency, which is why `test_dependency_constraints.py` exists. **Update
policy:** bounds are raised in a PR that runs the full suite against the new
version; Dependabot may propose, never auto-merge.

**This table is machine-checked.** `tests/test_dependency_constraints.py` parses
it and fails if it and `pyproject.toml` disagree in either direction, so a
docs-only edit here turns CI red. Change both in the same PR. `packaging>=24` is
deliberately one-sided — it is the only entry without an upper bound, because the
guard module needs a floor for `Requirement`/`SpecifierSet` behaviour and
`packaging` has no breaking-major cadence to defend against.

**The `mutation` extra is additive and is never installed alone.** `mutmut` drives
pytest over the L1 suite, which needs `hypothesis` and `pytest-asyncio`; mutmut's
own metadata declares neither. The nightly installs `.[dev,mutation]`. Installed
by itself it produces a mutmut whose test command fails at import, which surfaces
as "tests fail on unmutated code" rather than as a missing dependency.

**`mypy` tracks the current major, not the 1.x line.** An earlier draft of this
table said `>=1.11,<2`. mypy 2.0.0 shipped 6 May 2026 and 2.3.1 is current, so
that bound excluded every supported release of the tool. The 2.0 breaking changes
are dropping Python 3.9 as a runtime and as a `--python-version` target, and
enabling `--local-partial-types` and `--strict-bytes` by default. The first is
irrelevant to a `requires-python >=3.13` project; the second two change what mypy
reports, not what the code may express. §8A step 3's Protocol harness is the first
mypy harness in this repo, so writing it against 1.x defaults would only buy a
second migration later, against code written for the superseded behaviour. The floor is
the exact release verified rather than a bare `>=2`, matching how `mcp>=1.25` is
justified in `pyproject.toml`.

**No HTTP-mocking library.** `httpx.MockTransport` is already the pattern in
`test_searxng_unit.py:57` and covers every case here.

### 10.3 CI

| Job | Selection | Matrix | Platform |
|---|---|---|---|
| `fast` | `-m "not live and not subsystem and not chromium and not package"` | Python 3.13, 3.14 × mcp {min, max} | Windows + Linux |
| `fast-extras` | same, with `pdf-advanced` installed | Python 3.13 only | Linux |
| `subsystem` | `-m "subsystem and not chromium and not live"` | Python 3.13 | Windows + Linux |
| `chromium` | `-m "chromium and not live"` | Python 3.13 | Linux container |
| `package` | `tests/package -m package`, wheel build + install (§6.1) | Python 3.13 × mcp {min, max} | Linux |
| `live-serper` (nightly) | `-m "live and serper"` | — | Linux |
| `live-extraction` (nightly) | `-m "live and extraction"` | — | Linux container |
| `mutation` (nightly) | `mutmut run` over the §3.2 scope | — | Linux |
| `types` | `mypy` over the Protocol-carrying modules | Python 3.13 | Linux |
| `coverage` | pinned lane; runs the three controls of §10.4 | Python 3.13, pinned | Linux |
| `ci-required` | aggregation only — no tests | — | Linux |

`fast`, `fast-extras`, `subsystem`, `chromium`, `package`, `types` and `coverage`
run on every push and PR.

**The `coverage` job is where §10.4's controls actually execute**, and it is a
required dependency of `ci-required` (which remains the only *required check*).
Without it named here, a literal implementation of this document would produce
all-green required checks having run none of them. It cannot be folded into
`fast`: `fast` is a *compatibility matrix* over Python and `mcp` versions, while
these controls need exactly one pinned environment.

The job is deliberately **self-contained — it consumes no artefacts from other
jobs**:

1. Installs from `requirements-ratchet.txt` on Python 3.13, then makes the project
   itself importable — the lockfile deliberately excludes it, so an explicit step
   is required and the two commands belong together:

   ```bash
   pip install -r requirements-ratchet.txt
   pip install --no-deps -e .
   ```

   The job then asserts `kindly_web_search_mcp_server.__file__` resolves under the
   checkout's `src/`, which is the mirror image of §6.1's assertion and catches the
   opposite mistake: measuring an installed wheel when the lane is supposed to
   measure the working tree.
2. Runs the hermetic selection itself
   (`--ignore=tests/package -m "not live and not subsystem and not chromium and not package"`)
   under `coverage run`.
3. Runs all three controls of §10.4 against that single run.

No `needs` on the test jobs, no artefact upload or download, no cross-job
`coverage combine`. §10.4 explains why: a required threshold gate cannot take
nondeterministic input, and combining lanes that drive real browsers and timeouts
would make it one. Removing the fan-in also removes its whole failure surface —
unique artefact names per matrix leg, hidden-file exclusion, download collisions,
`coverage combine` replacing rather than appending, and every producer needing an
identical coverage version and `.coveragerc`. None of that machinery can silently
reduce the measurement to the hermetic lane, because the hermetic lane is all
there ever was.

**Workflow triggers must be complete.** Specifying `types` on `pull_request`
*replaces* the defaults rather than extending them, so listing only the label
events would stop the workflow running when a PR is opened at all — worst on a
fork PR, whose head branch produces no upstream `push` run:

```yaml
on:
  push:
    branches: [main]
  pull_request:
    types: [opened, reopened, synchronize, labeled, unlabeled]
```

Add `ready_for_review` if draft PRs are filtered out.

**One stable required check.** Requiring every matrix-generated check by name is
brittle: adding a Python or `mcp` axis renames the checks and silently drops the
old names from branch protection. `ci-required` runs no tests, declares `needs`
on the PR jobs, and fails unless every one of them succeeded. **It is the only
required check** under branch protection, so the matrix can change without
touching repository settings.

**`needs` alone is not enough, and getting this wrong is silent.** When an
upstream job fails, GitHub *skips* its dependents rather than failing them —
documented behaviour: "If a job fails, all jobs that need it are skipped unless
the jobs use a conditional statement that causes the job to continue." A skipped
required check does not report failure, so the aggregator must opt out of that
default and inspect results itself:

```yaml
ci-required:
  if: ${{ always() }}          # without this the job is skipped, not failed
  needs: [fast, fast-extras, subsystem, chromium, package, types, coverage]
  runs-on: ubuntu-latest
  steps:
    - name: Fail unless every dependency succeeded
      if: >-
        needs.fast.result != 'success' ||
        needs['fast-extras'].result != 'success' ||
        needs.subsystem.result != 'success' ||
        needs.chromium.result != 'success' ||
        needs.package.result != 'success' ||
        needs.types.result != 'success' ||
        needs.coverage.result != 'success'
      run: exit 1
```

**No expression substitution inside `run:`.** The comparison happens in the
Actions `if:` expression, and the shell step is a bare `exit 1`. Interpolating
`${{ toJSON(needs) }}` into a script — as an earlier draft did — is the pattern
GitHub warns against: the expression is substituted *before* the shell parses the
line, `needs` carries job **outputs** and not just results, and an apostrophe or
crafted output can break the quoting or inject commands. It also prints every
dependency's outputs into the log for no reason. Listing the jobs explicitly
costs a line each and keeps untrusted data out of the shell entirely.

Requiring `success` explicitly — rather than the looser
`!contains(needs.*.result, 'failure')` — is deliberate: the loose form treats
`skipped` as acceptable, which is exactly how a required job that quietly stopped
running would go unnoticed.

**Nightly jobs are not in `ci-required`.** They are `schedule`-triggered, so on a
PR they are skipped by definition. Including them forces a choice between an
aggregator that rejects skips (every PR red) and one that accepts them (a real
accidental skip hidden). The nightlies get their own `nightly-summary` aggregator
on the same pattern, which reports to the alert owner named in §6.3.

**The `chromium` job needs a test-runtime image, not the shipped one.** The
production `Dockerfile` starts `FROM python:3.13-slim` (a moving tag), installs
`chromium` from Debian's rolling archive, and `COPY`s `src/` in before
`pip install .`. Neither way of reusing it works: rebuilt per PR the browser and
base layer float, so the environment is not controlled; reused by digest it
contains a *previous* revision of the application and can go green **without ever
executing the PR's code** — the dangerous failure, because it reads as a pass.

So the job uses a **separate test-runtime image**, and the shipped `Dockerfile` is
left alone:

- It holds only Python, Chromium and system dependencies — **no application
  code**. Nothing in it changes when the project does.
- It is built from a pinned base digest with pinned Debian package versions (or an
  archive snapshot), published once, and referenced from CI **by digest**.
- CI installs the PR's wheel into it at run time (`pip install --no-deps` plus the
  pinned requirements), so the application layer is always the current checkout
  while the browser layer never moves.
- The job asserts the imported package resolves to that freshly installed wheel,
  as §6.1 requires. That assertion is what makes a green result impossible to
  obtain against code baked into the image.

Rebuilding the image is a deliberate PR that updates the recorded digest.

**`types` is deliberately narrow.** It runs `mypy` over the modules that declare or
implement the test-double Protocols (§8A step 3), not the whole tree. Repo-wide
type checking on ~6,849 largely unannotated lines is its own project with its own
justification; this job exists for one purpose — catching the signature drift that
`runtime_checkable` cannot see — and is scoped to earn its place immediately.
Widening it later is a separate decision.

**Matrix rationale and cost control.** `requires-python = ">=3.13"`, and
`pdf-advanced` is constrained to `python_version < '3.14'`, so 3.13 and 3.14
behave differently and both must be covered. `pdf-advanced` gets its own job
rather than a matrix axis so the absent-extras path — the default install — is
what the main matrix exercises.

The `mcp` {min, max} axis applies to **`fast` and `package`**. Restricting it to
`fast` would be wrong: §4.2's schema generation is not the only SDK-sensitive
surface — session initialization, transport negotiation, tool invocation and the
console entrypoints all come from the SDK, and SDK drift is the failure that
`test_dependency_constraints.py` was written for. `package` is where those are
exercised against a real install, so it carries the axis too. `subsystem` and
`chromium` do not, since their subjects are this project's own process and
browser handling. Windows is limited to `fast` and `subsystem`.

**Marker exclusions must be explicit and mutually exclusive.** `live-extraction`
needs a browser, so it would naturally carry `chromium` too — and the PR
`chromium` job would then run a billable network test on every PR. Every job's
selection therefore excludes `live` unless it is a live job.

**Path exclusion, not just markers, for installed-wheel tests.** A marker
exclusion only helps if the marker is present: a `tests/package/` file whose
author forgot `@pytest.mark.package` is still collected by
`-m "… and not package"` and would run against the source checkout, silently
proving nothing about the wheel. Therefore every source-checkout job passes
`--ignore=tests/package`, the `package` job targets `tests/package` explicitly,
and a collection-policy test asserts every test under that directory carries the
marker. Path and marker together; neither alone.

**Marker registration does not prove a marker is used.** `--strict-markers`
rejects *unknown* marks applied to tests; it says nothing about a job selecting a
marker no test carries.

**The exact failure mode is "too few", not "zero".** Measured, capturing pytest's
own exit status rather than a pipeline's:

| Command | Exit |
|---|---|
| `pytest -m typod` (nothing matches) | **5** — already fails CI |
| `pytest -m slow` (1 selected, 5 expected, no guard) | **0** — passes silently |

A selector matching *nothing* is safe by default: pytest returns
`EXIT_NOTESTSCOLLECTED` (5) and the step fails. The dangerous case is a selector
that matches a handful of tests when it should match forty — a renamed marker
left on two files, say. That exits 0 and the job is green.

Do **not** paper over this with `pytest … || [ $? -ne 5 ]`. The construct is
broken in both directions: on exit 0 the `||` short-circuits and the
under-selected run passes anyway, and on exit 1 — real test failures — the guard
runs, `[ 1 -ne 5 ]` succeeds, and a **failing job is converted into a green one**.

Enforce the count inside pytest, where collection is authoritative and the test
exit code is preserved:

```python
# tests/conftest.py
import pytest


def pytest_addoption(parser):
    parser.addoption("--min-selected", type=int, default=0,
                     help="Fail if fewer than N tests are selected.")


def pytest_collection_finish(session):
    minimum = session.config.getoption("--min-selected")
    if minimum and not session.testsfailed and len(session.items) < minimum:
        raise pytest.UsageError(
            f"selected {len(session.items)} tests, expected at least {minimum}; "
            "check the -m expression against the registered markers"
        )
```

**`pytest_collection_finish`, not `pytest_collection_modifyitems`.** Verified: the
`modifyitems` variant sees the full collected list *before* mark-based
deselection, so `len(items)` is 3 when `-m` selects none, and the guard never
fires. `collection_finish` reads `session.items` after deselection and reports the
true selected count.

**The guard stands down when the run has already failed.** `not
session.testsfailed` is not defensive padding; it is the difference between an
accurate alarm and a misleading one. Measured: a module that fails to import
leaves `session.testsfailed` at 1 *and* lowers `len(session.items)`, so without
the condition the guard fires on top of the collection error. The engineer's
headline becomes `selected 1 tests, expected at least 2; check the -m expression
against the registered markers` — blaming an innocent selector for an unrelated
import break, with the real `ModuleNotFoundError` buried above it, and pytest's
own exit 2 replaced by exit 4. The guard exists to turn a **green** run red. A
run that is already red needs nothing from it, and no safety is lost by the
silence: every suppressed case still fails the job.

**The option exists only for invocations targeting `tests/`.** `pytest_addoption`
is honoured in *initial* conftests only, and `tests/conftest.py` is one by virtue
of `testpaths = ["tests"]` and of any argument resolving under `tests/`. Measured
on pytest 9.1.1: `pytest --min-selected=1 src/` exits 4 with `unrecognized
arguments`, from inside the checkout and from outside it alike. Every job in the
table above selects by marker over `tests/`, so every one of them has the option;
the constraint binds nothing that exists today, and is recorded because a job
pointed anywhere else would fail with a message about a flag rather than about a
selector. `test_min_selected_guard.py` asserts the negative, so the day this
stops being true a test says so.

**A repository-root `conftest.py` was measured, not merely declined.** It does
register the option universally, but it solves nothing: with an argument of
`<checkout>/tests/package` the rootdir is still the checkout, so the root file
loads *and* `tests/conftest.py` loads, because it sits between the rootdir and
the argument. The package still resolved to the checkout's `src/`. The cost of
the extra file is real but small — hatchling builds the wheel from `src/`, so a
root file would reach only the sdist, which already carries `tests/conftest.py` —
so the reason to reject it is simply that it buys nothing.

**Unresolved, and named here rather than discovered in CI: `tests/conftest.py`
shadows any installed copy of the package.** Line 8 of that file inserts
`<checkout>/src` at `sys.path[0]` and line 10 then imports the package, caching
the checkout's copy in `sys.modules` before the first test runs. Position 0 beats
site-packages, so this happens from **every** working directory. §6.1's "run from
outside the checkout" is necessary but not sufficient. Measured from a directory
outside the checkout with the package installed in the active environment:
`kindly_web_search_mcp_server.__file__` resolved to `<checkout>/src/…`, and the
result was identical with `--min-selected` present and absent — so this is **not**
a consequence of the floor. The floor merely made the collision visible, by
forcing the question of how a job outside `tests/` would spell its target.

Two assertions in this document therefore cannot hold as written while that line
stands: §6.1's "`__file__` under the venv's `site-packages` and not the checkout"
for the `package` job, and the identical claim made above for the `chromium` job,
which selects `-m "chromium and not live"` over `tests/` and so loads the same
conftest. The mirror-image assertion in the `coverage` job's step 1 is affected in
the opposite direction: it passes *because* of line 8, whether or not the editable
install succeeded, so it currently measures nothing.

**The resolution belongs to the step that builds the wheel harness, not here.**
This step neither introduced the conflict nor can settle it without changing the
`sys.path` convention every existing test module depends on, and settling it in a
bootstrap step would be a decision made where its consequences cannot be observed.
Two candidates were measured and both work, so the choice is a decision and not a
research task:

- **An explicit opt-out on the insert.** `tests/conftest.py` skips line 8 when the
  job sets, say, `KINDLY_TEST_INSTALLED=1`. The installed copy then wins,
  `--min-selected` still exists, and no other invocation changes. It is the only
  candidate that also fixes `chromium`, whose tests stay under `tests/`. An
  implicit form — insert only if the package is not already importable — is
  rejected: a developer with a stale `pip install .` in their virtualenv would
  silently start testing the wrong tree.
- **Moving the wheel tests to a sibling of `tests/`.** Works, and plain `pytest`
  is unaffected because `testpaths = ["tests"]`. But it contradicts the
  `tests/package/` location this document assumes throughout, including every
  `--ignore=tests/package`, and it leaves `chromium` broken.

`--confcutdir=<checkout>/tests/package` also works — the parent conftest is not
loaded and the installed copy wins — but only if the option is registered in
`tests/package/conftest.py`, and registering it in both files is a hard
`ValueError: option names {'--min-selected'} already added` on any run that loads
both, including a bare `pytest`. Recorded so it is not tried a second time.

Until that is settled, **this section deliberately does not tell the `package` job
how to spell its target.** An earlier draft did, and the advice was wrong: it
would have set that job up to fail its own §6.1 assertion.

Only a positive floor arms the guard. Zero is the default and disables it, which
is what keeps every existing invocation unaffected; a negative value disables it
just as silently, so a job computing its floor in the shell should not let one
through.

Every marker-selected job passes `--min-selected=<n>`. Measured behaviour:
under-selection fails at collection (exit 4, with the count in the message),
while a genuine test failure keeps its own exit 1 — no shell arithmetic in
between.

**A floor detects loss, not omission, and needs a maintenance rule.** If forty
tests match today and ten *new* tests are added without the marker, a minimum of
forty still passes. So: the expected minima are committed alongside the workflow,
and any deliberate addition or removal of tests in a marked area updates the
count in the same PR, where a reviewer sees it. The count is a tripwire for
accidental loss, not a census.

**Prefer a policy test where ownership is structural.** For a directory with a
single rule — every test under `tests/package/` carries `@pytest.mark.package` —
a test that walks the directory and asserts the rule is strictly stronger than a
count: it catches the newly-added unmarked test that a floor cannot see. Use
counts only where membership is scattered across the tree and no such rule
exists.

**Secrets.** `SERPER_API_KEY` is a repository Actions secret.

- **Never expose it to untrusted code.** Secrets are withheld from `pull_request`
  runs from forks, which get a read-only `GITHUB_TOKEN` — the correct default,
  and relevant here because PR #50 came from a fork. Live jobs run on `schedule`
  and `workflow_dispatch` against `main`, never on fork PRs. Do not "fix" this
  with `pull_request_target`; that is the documented "pwn request" pattern.
- **Fail loudly when missing.** `live-serper` asserts the secret is present
  before collection, so a rotated-and-not-updated key gives a red nightly rather
  than a green skip.
- **The key is billable.** One query per provider per night (§6.2); breadth
  belongs in the mocked L1 provider tests, which cost nothing.

**"Green" is a gate only when enforced.** A passing workflow is not a gate until
`ci-required` — and only `ci-required`, per the paragraph above — is a **required
check** under branch protection on `main`, with `fast`, `fast-extras`,
`subsystem`, `chromium`, `package`, `types` and `coverage` in its `needs`.

### 10.4 Coverage

No absolute percentage target: a percentage rewards executing lines, and it rises
when you delete uncovered production code — a refactor that removes a dead branch
improves the number without improving the testing. Three controls instead, each
aimed at a failure the others cannot see.

**1. Every production module must appear in the report.** This is the control that
would actually have caught this project's worst gap, and the first draft of this
section did not have it.

By default coverage.py reports only files it *observed being executed*. A module
no test ever imports is absent from the report entirely — so
`scrape/chromium_pool.py`, 372 lines with no test file, would contribute nothing
and drag nothing down. It would be invisible rather than visibly at zero. Setting
`source_pkgs` is what makes coverage.py report never-executed files.

Three configuration files, because the jobs genuinely need different behaviour and
an earlier draft described settings that were not in the file it displayed:

```ini
# .coveragerc — base, used by the gating lane's control 1 view
[run]
source_pkgs = kindly_web_search_mcp_server
branch = true
relative_files = true

[paths]
source =
    src/kindly_web_search_mcp_server
    */site-packages/kindly_web_search_mcp_server
```

```ini
# .coveragerc-gate — controls 2 and 3; base plus the declared exemptions
[run]
source_pkgs = kindly_web_search_mcp_server
branch = true
relative_files = true
omit =
    */scrape/nodriver_worker.py
    */scrape/chromium_pool.py
    */scrape/worker_runner.py

[paths]
source =
    src/kindly_web_search_mcp_server
    */site-packages/kindly_web_search_mcp_server
```

```ini
# .coveragerc-subprocess — observational L3 jobs only
[run]
source_pkgs = kindly_web_search_mcp_server
branch = true
relative_files = true
parallel = true
patch = subprocess
```

Each job selects one by **absolute `COVERAGE_RCFILE`**, never by discovery.
Discovery does *not* walk up the tree — an earlier draft said it did. coverage.py
looks only in the directory it is run from, trying `.coveragerc`, then
`.coveragerc.toml`, `setup.cfg`, `tox.ini` and `pyproject.toml`, and takes the
first that carries coverage settings. §6.1 deliberately runs the package tests
from outside the checkout, so none of ours would be found there — and the failure
is worse than falling back to defaults, because a `setup.cfg`, `tox.ini` or
`pyproject.toml` belonging to some unrelated directory is a perfectly acceptable
answer to that search. `COVERAGE_FILE` is likewise set to an absolute path.

One more reason the path must be absolute, and a genuinely surprising one:
`config_files_to_try` special-cases the literal string `".coveragerc"` to mean
"no file specified", and the unspecified branch then reads `COVERAGE_RCFILE`
from the environment. Passing the bare relative name therefore does not select
this repository's file — it selects whatever the environment already pointed at.

`patch = subprocess` implies `parallel = true`, so those jobs write suffixed data
files and **must run a local `coverage combine` before reporting** — reporting does
not implicitly combine them. That combine is local to the job; it is not the
cross-job fan-in removed above.

The `[paths]` mapping stays in the gating configs even though that lane runs from a
source checkout, because §6.1's package tests do install a wheel and their
observational report needs the mapping to line up with the tree.

**`.coveragerc-subprocess` has no `[paths]` mapping, and one of its two consumers
is a wheel job.** `subsystem` runs from a source checkout, where recorded paths
already match the tree; `chromium` does not — §10.3 has it install the PR's wheel
and assert the package resolves to that wheel, so its paths land under
`site-packages` with nothing mapping them back to `src/…`. This is accepted, not
overlooked: that report feeds neither `diff-cover` nor the committed baseline,
which are the two places an unmapped path silently reads as "no coverage". The
cost is confined to the per-module summary this lane publishes, whose module
names come out wheel-shaped. If that summary is ever wanted keyed to source
paths, the mapping has to be added here — a change to this section, not a fix to
make in passing.

The `[paths]` mapping matters wherever a wheel is involved. The gating lane runs
from a source checkout, so its own paths already match `git diff`; §6.1's package
tests install a wheel and record paths under `site-packages`, and unmapped those
never join `src/kindly_web_search_mcp_server/...` — the totals double-count and
`diff-cover` finds no coverage for any changed line, passing silently.
`relative_files = true` keeps paths comparable between jobs and platforms.

Three assertions enforce it. Note that "appears in the report" is **not** one of
them: under `source_pkgs` every module always appears, so such a check can only
fail on a broken config and says nothing about whether tests exist. The control
has to assert coverage, not presence.

- **Classification is total and exclusive.** Every `src/**/*.py` file is either in
  the gating scope or in `.coveragerc-gate`'s `omit` list — exactly one. A policy
  test walks the tree and the omit list and fails on any file in both or neither,
  so a new module cannot slip in unclassified.
- **Every gating-scope module has non-zero coverage** in the hermetic report, and
  **every omitted module has non-zero coverage** in the observational L3 report.
  This is what would have caught `chromium_pool.py`: it is classified L3, the L3
  report shows it at zero, the build fails until a test exists. Presence never
  would have.
- **If the diff contains at least one changed line the report represents as a
  statement**, `diff-cover` may not report zero analyzed lines — that means the
  path mapping broke, not that the change was safe. The condition is necessary
  rather than pedantic: a PR editing only a literal on a continuation line of a
  multi-line call legitimately has no represented statement lines, and an
  unconditional guard would misdiagnose that as broken wiring.

**One coverage run, two report views.** Every control measures the `coverage`
job's own pinned run of the L1/L2 selection; nothing is combined across jobs. But
that single run is reported twice, and the difference is load-bearing.

`source_pkgs` makes the report **whole-package by definition** — that is the point
of control 1, and it is also a trap. A module the hermetic lane cannot execute,
such as `chromium_pool.py`, appears with every statement at zero hits. It is not
absent; it is present and uncovered. So a scope claim cannot be made by wishing:
without filtering, `diff-cover` sees those zero-hit statements and counts changed
ones as uncovered, and adding L3-only statements grows the ratchet's denominator
while the numerator stands still. Ordinary worker or pool development would fail
both gates, pushing authors either to write hermetic tests for code that has no
hermetic seam or to reach for the baseline-reset escape hatch. An earlier draft of
this section asserted such lines were "not counted for or against" the gate; that
was false, and this is the correction.

**The gating scope is therefore declared, and the declaration is checked.**

| View | Config | Feeds |
|---|---|---|
| whole-package | `.coveragerc` | control 1 |
| gating scope | `.coveragerc-gate` — the same file plus an `omit` list | controls 2 and 3 |

`omit` names the modules with no hermetic seam. Today that is
`scrape/nodriver_worker.py`, `scrape/chromium_pool.py`, and `scrape/worker_runner.py`
— the process-management module §11.2 extracts from `universal_html.py`.

**That extraction is a prerequisite of this control, not a nicety** — see §11.2
for why file-granularity `omit` forces it, and why the split is the same argument
§2.1 makes about assertions: the seam belongs in the design, not in a side-table.

The alternative was combining every lane into the diff gate, and it does not
survive contact with the arithmetic. A required threshold gate must take
deterministic input. On a five-statement diff, one timing-dependent line moves the
result from 80% to 60% — so feeding lanes that drive real browsers, timeouts and
retries into an 80% required check produces a check that goes red for reasons
unrelated to the change. An earlier draft justified this as jitter "rarely"
flipping the threshold; "rarely" is not a property a required check can be built
on, and a flaky required check is precisely the normalised-red failure this whole
document exists to prevent.

The full `subsystem` and `chromium` coverage is still produced as
**observational** reporting. It does not gate — but "observational" cannot mean
"emitted and forgotten", because this project's founding problem was exactly that:
a signal nobody read. Unowned reporting decays into the same thing.

So it is specified rather than gestured at:

- Published as an HTML and JSON artefact per run, with `if-no-files-found: error`,
  so a silently missing report fails the job instead of reading as "nothing to
  say".
- Reduced to a **per-module summary in the PR job summary** — module, percent,
  delta against the previous run on `main`. A number in a downloadable artefact is
  not a signal; a table on the PR is.
- Control 1's second assertion already gates the strongest fact this report
  carries — that an L3-classified module has *some* coverage — so the observational
  view is for degree, not existence.
- It has a **named reviewer and a cadence**, assigned when §13's owner decision is
  resolved. Until then it is explicitly unowned, which is a known gap rather than
  an oversight.

**2. Diff coverage — the PR gate.** `diff-cover --fail-under=80` over the changed
lines of each PR.

The promise is precise: **at least 80% of changed statement lines that appear in
the hermetic coverage report must be covered.** Lines in files the hermetic lane
cannot exercise are not counted for or against it — see the product note above. Not every changed line — diff-cover
compares a line-based diff against a statement-based report, so continuation lines
of a multi-line statement appear in the diff and not in the report, and are simply
not counted. `--expand-coverage-report` extends the report to cover them; it is
worth adopting, but it must be validated on this codebase first and the promise
above is what holds without it.

Operationally:

- The `coverage` job exports its run as `coverage.xml`; `diff-cover` consumes
  that, not the console report.
- **The diff comes from the same boundary as the baseline** — the event's base
  SHA, not `origin/main`:

  ```bash
  git diff "$BASE_SHA"...HEAD > diff.txt
  diff-cover coverage.xml --diff-file=diff.txt --fail-under=80
  ```

  `--compare-branch=origin/main` would contradict the stacked-PR rule below: a PR
  based on another PR would be charged for its parent's changed lines. One
  boundary for both controls, from the immutable event payload.
- `actions/checkout` defaults to a shallow clone in which the base SHA is absent
  and `git diff` cannot resolve it. The job sets `fetch-depth: 0`, or fetches the
  base explicitly.
- The gate measures changed-line execution only. Branch completeness on changed
  lines needs `diff-cover >= 10.4` and `--branch-coverage`, which this design does
  **not** pass; §10.2 sets the floor at 10.4 anyway so the lane's exact pins do not
  have to be renegotiated later to gain the option. Adopting the flag is a separate
  change that must be exercised first.

**Instrumenting the worker subprocess.** §5.2 runs
`python -m …scrape.nodriver_worker` as a real child, and running the *parent*
under coverage does not instrument it — so without configuration the worker's own
code reads as uncovered no matter how thoroughly the subsystem tests exercise it,
and a PR touching it would be charged by control 2 for lines it cannot cover.

The `subsystem` and `chromium` jobs therefore run under
`.coveragerc-subprocess`, which sets `parallel = true` with `patch = subprocess`,
and `coverage combine` the child data files before publishing their
**observational** report. The floor is **7.10.3**, not
7.10.0: the option arrived in 7.10.0, but 7.10.3 fixed missed nested children, data
stranded when a child changes working directory, Windows startup failures and
incomplete configuration propagation to children — every one of which this design's
worker tests exercise. `COVERAGE_PROCESS_START` with a `.pth` hook is the
alternative if that floor is unacceptable.

**A forcibly killed child does not flush its coverage data.** The
kill-at-the-deadline and terminate-the-process-tree tests of §5.2 will therefore
contribute little or nothing for the worker, by construction. That is a real and
irreducible limit: those tests exist to prove a process dies, and a dead process
cannot report. Do not chase the resulting gap with a coverage exclusion that also
hides genuinely untested code — measure it, note it, and let the real-child
assertions of §5.2 carry the confidence there. Mutation testing cannot help here:
§3.2 deliberately scopes `mutmut` to the pure-logic modules and excludes `scrape/`
plumbing, so it has nothing to say about these paths. Widening that scope is a
separate decision with its own runtime cost, not an answer to this gap.

**3. A committed baseline over the hermetic tests only.** Total branch coverage of
the hermetic suite is written to `coverage-baseline.json`, committed, and may not
decrease.

**Scope: the gating scope only, measured on Linux, Python 3.13, pinned lane.**
This follows from the single-product decision above rather than adding a rule:
every control measures the hermetic lane, so the ratchet inherits its determinism.
Were the nondeterministic lanes included, a single differently-taken branch would
move a two-decimal total, and ratcheting on a flickering number teaches people to
edit the baseline until CI passes — which destroys the control.

**Stability is an acceptance criterion, not an assumption.** Before check (b)
below is switched on, run the pinned lane repeatedly on one commit and compare the
**covered and missing line and branch sets**, not the rounded percentage. Equal
percentages can hide compensating differences. If the sets differ, the offending
tests are stabilized or dropped from the ratcheted set before exact equality is
enabled.

The checks:

- **(a) `head_baseline >= base_baseline`**, with the base value read from the
  event's base SHA — `git show "$BASE_SHA":coverage-baseline.json` — not
  `origin/main`, whose tip can move mid-workflow. **Exception:** a decrease is
  permitted only when the PR carries the `coverage-baseline-reset` label. CI reads
  the label from the event and applies it as a condition, so an authorized reset
  passes mechanically instead of being worked around. See the reset rule below.
- **(b) `measured_head == head_baseline`** exactly, under the definition below.
  Without it a
  rise need never be recorded, and a later PR could give the gain back while still
  clearing a stale floor.
- **(c) `measured_head >= head_baseline`**, implied by (b) but reported separately
  so a precision mismatch reads as such rather than as a regression.

**The metric, defined to the machine.** "Two decimal places" is not a
specification, and float equality is not a comparison anyone should rely on:

- **Source of truth:** the integer totals in `coverage json` output —
  `covered_lines`, `num_statements`, `covered_branches`, `num_branches`. Not
  `percent_covered` (a binary float) and not `percent_covered_display` (a
  presentation string).
- **Definition:** coverage.py's combined statement-and-branch measure —
  `(covered_lines + covered_branches) / (num_statements + num_branches)` — stated
  explicitly because "branch coverage" is ambiguous between this and a
  branches-only ratio.
- **Comparison unit:** integer **basis points**, computed as
  `(covered * 10000) // total`. Integer arithmetic throughout; no floats, no
  rounding mode to argue about.
- **`coverage-baseline.json` schema:** `{"basis_points": <int>, "covered": <int>,
  "total": <int>, "generated_by": "<tool versions>"}`. The raw counts are stored
  alongside the ratio so a reviewer can see *what* moved, not merely that it did.
- **What equality covers:** `basis_points`, `covered` and `total` must all match
  the measured run. Comparing only `basis_points` would let the raw counts drift
  stale and hide compensating changes — a file removed and another grown can hold
  the ratio while both numbers move. `generated_by` is **not** part of the equality
  check; it is validated separately against the pinned lane's configuration, so a
  tooling bump reports as a tooling mismatch rather than as a coverage regression.

**Bootstrap and non-PR cases.**

- **Bootstrap.** When `coverage-baseline.json` is absent from the base SHA, skip
  (a) and require only (b). Absent is not zero — treating it as zero would let any
  value through as an improvement.
- **Pushes to `main`.** Run (b) and (c) only. There is no base to ratchet against
  and the value was gated on the PR that merged it — **but that holds only if every
  change reaches `main` through a PR.** "Require a pull request before merging" is
  a separate branch-protection setting from required status checks, and bypass
  actors can push directly. So either make *require a pull request, no bypass
  actors* part of the required repository configuration alongside `ci-required`, or
  compare a `main` push against its **first parent's** baseline. The first is the
  recommendation; the second exists so the rule is not silently void without it.
- **PRs targeting other branches.** Unchanged, because the rule reads the event's
  base SHA rather than a hard-coded branch. A stacked PR ratchets against its own
  parent.

**The reset rule, as an implementable condition.** A `coverage` or Python update
can lower the number on identical code, where (b) demands a lower baseline and (a)
refuses it. Left as prose this deadlocks the first dependency bump. As a
mechanism:

- Check (a) fails on a decrease **unless** the PR carries the
  `coverage-baseline-reset` label. §10.3's trigger list is canonical and already
  includes the label events, so applying the label reruns the check rather than
  requiring a manual re-run. Do not restate the trigger here — an earlier draft
  did, and the copy drifted into an incomplete list that would have stopped the
  workflow running on PR open.
- Authorization is on the **merge**, not on the label. `CODEOWNERS` governs who
  must approve a change to `coverage-baseline.json` and `requirements-ratchet.txt`;
  branch protection requires that code-owner review and dismisses stale approvals
  on new commits; `CODEOWNERS` is owned by the same people. It does **not** control
  who may apply a label — anyone with write access can. The label is a signal that
  unblocks the check; the code-owner approval is what actually authorizes the
  reset. If label application must itself be restricted, the workflow has to verify
  the actor's team membership explicitly, which is additional machinery this design
  does not currently require.
- The PR must state what changed in the measurement and why the drop is not a loss
  of testing. The default expectation remains that the author recovers the
  difference.

**Measurement environment for the baseline.** Dependencies come from the committed
`requirements-ratchet.txt` — the single authority for this lane, not
`requirements.txt`, which covers runtime only and has no `pytest`, `coverage` or
`diff-cover`. Pinning those with `==` while leaving their transitive dependencies
free would reopen the same hole one level down.

`requirements-ratchet.txt` is regenerated deliberately, and its header records how:
install a `ratchet` extra — declaring pytest, pytest-asyncio, hypothesis, coverage,
diff-cover, mypy and packaging — into a clean Python 3.13 Linux virtualenv, then
`pip freeze --exclude-editable` with the project itself filtered out, since a
`pip freeze` after `pip install .[…]` otherwise records a local path or URL that no
other machine can resolve. Add `--require-hashes` if this lane is ever treated as
security-relevant; reproducibility alone does not need it.

`packaging` is in that list for the same reason it is in `dev`:
`test_dependency_constraints.py` imports it directly, and a directly-imported
package that arrives only transitively is precisely the failure that guard exists
to catch. It *would* resolve through `pytest`, whose metadata requires
`packaging>=22` unconditionally — so declaring it changes nothing about whether
the lane runs, and everything about whether the declaration is honest. `ruff` and
`mutmut` stay out: `ruff` is lint rather than measurement, and `mutmut` is
Linux-only and belongs to the nightly.

`mypy` is in the pinned lane on the assumption that E2-1's negative type-check
fixture runs inside the ratcheted selection. If E2-1 marks that fixture
`subsystem` — §10.5 defines the marker as needing "a real socket or child
process", and the fixture shells out to mypy — then it leaves the hermetic set and
`mypy` should be dropped from both this extra and the lockfile. **E2-1 must state
the marker explicitly**; the two readings disagree and nothing else settles it.

**The lockfile is the whole environment, not just the tooling.** §10.3's lane
installs the project with `--no-deps`, so nothing else resolves `mcp`, `httpx`,
`nodriver` or `trafilatura` — they are pinned here too. That is deliberate, and it
has a consequence the reset rule above must absorb: **a runtime dependency bump
moves the baseline**, because a different `mcp` or `nodriver` takes different
branches in the hermetic suite. The reset trigger is therefore a `coverage`,
Python, **or pinned runtime-dependency** update, not the first two alone. Where
this file and `requirements.txt` disagree, this file wins for this lane; they are
permitted to diverge.

Adopting `--require-hashes` also means relaxing
`test_ratchet_lockfile_pins_every_entry`, which currently requires every line to
be a bare `name==version`; a hash-pinned file is a continuation line plus
`--hash=` entries. Change both in the same PR.

The exact `coverage` pin is raised only in a labelled `coverage-baseline-reset`
PR, and the baseline is only ever measured from `requirements-ratchet.txt` —
never from `.[dev]`, whose `>=7.10.3,<8` range resolves higher and would report a
different number locally than CI does.

Because the ratcheted set is `fast` only, no browser image is involved and the
test-runtime image digest cannot move the baseline. That is a further reason to
scope it this way.

**What each control would have caught.** The stale tests raise `TypeError` before
`_fetch_html`'s body runs, so its lines would show dark in a report — but only to
someone who looked, and a whole-project total might not move enough to trip a
ratchet. Control 1 is what makes an untested module impossible to overlook;
control 2 is what stops new code arriving uncovered; control 3 is what makes
silent erosion fail; mutation testing (§3.2) is what judges whether the assertions
mean anything. None substitutes for another.

### 10.5 pytest configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
strict_markers = true
strict_config = true
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = [
    "live: hits the real network; needs KINDLY_RUN_LIVE_TESTS=1",
    "subsystem: needs a real socket or child process; portable",
    "chromium: needs a real browser; Linux container only",
    "package: runs against an installed wheel, not the source tree",
    "serper: live test requiring SERPER_API_KEY",
    "extraction: live content-extraction canary; browser, no credential",
    "slow: over a second",
]
```

**This block is machine-checked.** `tests/test_pytest_configuration.py` parses
it out of this section and fails if it and `pyproject.toml` disagree in either
direction, so a docs-only edit here turns CI red. Change both in the same PR.
The guard requires this section to keep its heading and to contain exactly one
fenced `toml` block. `pyproject.toml` may carry comments the block does not; the
comparison is between parsed tables, not text.

**`strict_markers`, not `addopts = "--strict-markers"`.** An earlier draft of
this section used the flag. pytest **9.0.0 through 9.0.3 silently ignore**
`--strict-markers` and `--strict-config` when they arrive through `addopts`
([pytest#14442](https://github.com/pytest-dev/pytest/issues/14442), fixed in
9.1.0), and §10.2's bound is `pytest>=9,<10`, so those releases are installable.
Measured across 9.0.0, 9.0.1, 9.0.2, 9.0.3, 9.1.0 and 9.1.1: every 9.0.x lets an
unregistered marker pass at exit 0 under the `addopts` form, while
`strict_markers = true` aborts collection at exit 2 on all six. The failure the flag exists
to prevent is precisely a silent one — a marker accepted but unregistered labels
a test that every marker-selected job then skips, while it still reports green —
so a spelling that can itself fail silently is the wrong one. The ini options
were added in pytest 9.0 (#13823) and work across the whole allowed range;
upstream recommends them when targeting pytest ≥ 9. The same release added a
`strict = true` umbrella covering `strict_config`, `strict_markers`,
`strict_parametrization_ids` and `strict_xfail`. It is **not** used here: it
opts this repository into every strictness option pytest adds in future, which
upstream itself says to take only on a pinned version. §10.2 deliberately bounds
rather than pins, so the two options wanted are named explicitly.
`test_pytest_configuration.py`
asserts no `--strict*` entry returns to `addopts`, because CI resolves a pytest
where both spellings work and would not otherwise notice.

**`strict_config` turns a missing plugin into an error rather than a symptom.**
Without it, an environment lacking `pytest-asyncio` degrades `asyncio_mode` to a
`PytestConfigWarning` and then fails every async test with `async def functions
are not natively supported` — a symptom three steps from its cause. With it the
run stops at `ERROR: Unknown config option: asyncio_mode`.

**`asyncio_default_fixture_loop_scope` is set because unset is not neutral.**
pytest-asyncio 1.4 raises a `PytestDeprecationWarning` when it is unset,
announcing that a future release will change the async-fixture event-loop
default from the `fixture` caching scope to `function`. `pytest-asyncio>=1.4,<2`
admits that release, and it would land on §10.1's migration, which converts the
whole suite at once. The warning is raised during `pytest_configure`, before the
warnings plugin captures, so it never reaches the warnings summary and nobody
would see it coming. `"function"` is the announced future default, chosen so the
change is a no-op here.

**Auto mode changes nothing about the existing suite.** pytest-asyncio does not
handle `unittest` classes, so the `IsolatedAsyncioTestCase` tests are unaffected
and the counts in §1.1 are unchanged by this block. Its effect is on the plain
`async def` tests §10.1's migration produces. Measured on pytest 9.1.1: an
unmarked `async def` test **fails** with `async def functions are not natively
supported` when the mode is unset — it does not pass silently — so
`test_pytest_configuration.py` carries one unmarked `async def` case whose
passing is the observable that auto mode is in effect.

---

## 11. Production seams this design requires

This test design cannot be implemented against the code as it stands. Three
changes are prerequisites, listed here so they are scheduled rather than
improvised as monkeypatches during implementation.

### 11.1 Provider selection must be reachable by configuration

Needed by §6.1. No change to shipped code is required — `SEARXNG_BASE_URL` plus
cleared higher-priority variables is sufficient, because `search_searxng` is
configured entirely by URL. **The requirement is that this stays true**: any
future provider-selection change must preserve a URL-configurable provider, or
§6.1 loses its only cross-process seam. Recorded as a constraint, not a code
change.

### 11.2 The worker command must be injectable

Needed by §5.2. `fetch_html_via_nodriver` hardcodes its child command
(`universal_html.py:550`):

```python
base_cmd = [executable, "-m", "kindly_web_search_mcp_server.scrape.nodriver_worker", ...]
```

There is no way to point it at a fixture child. Split the function in two:

- `_build_worker_command(...) -> list[str]` — the production command builder, a
  pure function that L1 asserts on directly.
- `_run_worker_command(command, *, timeout, diagnostics, ...)` — **private** —
  containing the spawn, stream, heartbeat and termination logic that §5.2 tests.

`fetch_html_via_nodriver` keeps its current signature and **always builds its own
command**, calling the builder and passing the result to the runner. Tests reach
`_run_worker_command` directly with a fixture command.

**Move the runner into its own module, `scrape/worker_runner.py`.** This is not
tidiness: §10.4 classifies every production file as hermetically testable or not,
and `omit` works at file granularity. `universal_html.py` today mixes the
Markdown-suffix probe path — covered by 15 passing hermetic tests — with subprocess
spawn and stream management that only a real child can exercise. While both live in
one file, the coverage gate must treat the whole file as one or the other, and both
choices are wrong: gate it and worker changes fail against a lane with no seam for
them; exempt it and the probe path loses real gating. The split makes the
classification a module boundary, which is a design property rather than a
side-table to maintain.

`_build_worker_command` stays with `fetch_html_via_nodriver`; it is pure and
hermetically testable, so it belongs on the gated side.

**The command must not become a parameter of the public fetch API.** Adding a
`command=` argument to `fetch_html_via_nodriver` would turn "execute an arbitrary
process" into a supported input of a function whose URL argument is already
attacker-influenced (§7.2). Keeping the seam private and the public entry point
command-free costs nothing and closes that path.

The alternative — monkeypatching `asyncio.create_subprocess_exec` — reproduces
exactly the opaque coupling that let `_FakeProc` drift, and is ruled out.

### 11.3 Tool result schemas are absent by construction

Relevant to §4.2. Both tools are annotated `-> dict`, so `outputSchema` is
`null` and clients receive no result contract. The tests in §4.2 pin the current
state honestly. Giving clients a real schema means annotating the tools with
their response models — a production API change with client impact, listed for
decision in §13.

---

## 12. Design decisions worth recording

**Assertions live at the layer that owns them, and split when a test spans two
(§2.1, §8A).** The eight sandbox tests mix flag resolution with retry
orchestration. Moving the whole file down would lose the orchestration; leaving
it up preserves the coupling that broke it. Splitting is more work than either
and is the only option that loses nothing.

**Real child process *and* narrow doubles at L3 (§5.2).** A fixture child cannot
assert which flags a call produced; a double cannot assert a process tree died.
The two are complementary. A fixture child can itself drift from the real worker,
so this is a stronger guarantee than a hand-written fake, not an absolute one.

**Saved-HTML corpus instead of live extraction tests (§3.1, §3.3).** Pages
change, so live extraction assertions either weaken to uselessness or flake.
Fixing the input moves most of it to L1; the §6.2 canaries detect corpus
staleness.

**Sanitization at the boundary, before `entries` is appended (§7.1).** Relying on
call sites fails the first time someone adds an `emit`. Sanitizing only at the
stderr writer would leave the MCP response — the more exposed path — raw.

**Configuration, not a production hook, for cross-process stubbing (§6.1).** An
injection point in shipped code is a permanent risk on an unauthenticated server;
a local SearXNG-contract server is a test-only artefact.

**No absolute coverage target, but an enforced diff-coverage gate and a committed
baseline (§10.4).**
Stated explicitly so the omission is not mistaken for an oversight.

---

## 13. Decision records and external prerequisites

Not everything here is open, and not everything open is here. The four states:

| State | Items |
|---|---|
| **Externally unresolved** — nobody has decided | §13.1 (X-1), §13.3 (X-6) |
| **Provisionally resolved** — settled for this implementation, revisitable | §13.2 |
| **Scheduled** — deferred deliberately, with a step that discharges it | §13.4 |
| **Deferred with a reason** — not scheduled, and why | §13.5 |

**Operational prerequisites live only in the plan.** X-2 (repository admin), X-3
(container registry), X-4 (live-query budget) and X-5 (observational coverage
owner) are provisioning and ownership tasks, not design decisions, so they are
tracked in `TEST_SUITE_IMPLEMENTATION_PLAN.md` §3 and are deliberately absent
here.

**For the items that are here, this section is authoritative.** The plan's §3
records the same decisions for *scheduling* — owner, blocked steps — but not what
is being decided. If the two disagree about substance, this section is right and
the plan is stale.

**Review points we decide not to act on are recorded inline** as `> **Deferred:**`
blocks at the place they apply, naming what was raised and why it was not taken.
A judgement that lives only in a review thread is invisible to the next reader.

### 13.1 Outbound request policy — RESOLVED 2026-08-31

**The policy is stated in §7.2.** In summary: `http`/`https` only; public addresses
only for externally-supplied URLs; unrestricted for operator-configured ones;
`httpx` fully enforced; Chromium enforced pre-navigation with the rebinding and
subresource gaps accepted and characterized; `KINDLY_CHROME_PROXY` a documented
trusted boundary.

**What made this decidable** was separating two things that had been conflated:

- **Scheme and address are independent dimensions.** A single allow/restrict switch
  could not express the answer that was actually wanted — refuse exotic schemes,
  restrict addresses by provenance.
- **Provenance matters more than the address class.** "Private addresses must be
  allowed or self-hosted SearXNG breaks" was true of the *operator-configured*
  path and irrelevant to the *externally-supplied* one. Those are different code
  paths, so a restriction on the second costs the first nothing. That observation
  is what turned a decision that looked expensive into a cheap one.

**Deliberately not chosen:** the local policy-enforcing proxy. It is the only
mechanism that would close the Chromium subresource and rebinding gaps, and it was
judged disproportionate for this server's exposure. §7.3 records the conditions
that should reopen it.

> **Deferred: further policy dimensions.** Redirect depth, non-standard ports,
> request method and response size could each be carved out as dimensions in the
> same way. They are not, because no restriction on them has been asked for and
> each speculative one adds a column an answer must fill. Adding one later is a
> paragraph here and a case in §7.2.

### 13.2 Tool result schemas — no external prerequisite

Annotate `web_search` and `get_content` with their response models so clients
receive an `outputSchema`, or keep `-> dict` and pin its absence? An API change
with client impact either way.

**Resolved for this implementation, provisionally:** `outputSchema is None` is
**normative** and §4.2's golden test pins it. That keeps the contract test honest
about what clients receive today. Revisiting is a new step, not a pending decision
blocking anything — which is why this has no X-number.

### 13.3 Owners in the risk matrix — X-6

§9's Owner column is blank except where X-4 (live-alert) and X-5 (observational
coverage) name someone. Assigning the rest is not this document's call.

**An answer must** name an owner per row, and be **written into §9** — the plan's
**E13-2** does that, because an answer living only in a ticket leaves the
authoritative design showing an empty column.

**Blocks:** completion (the plan's E13-1) via E13-2. It blocks no implementation
work.

### 13.4 Per-module coverage minimums — no external prerequisite

Diff coverage and the committed baseline are adopted (§10.4). Fixed per-module
floors are not, because no coverage measurement exists in this repository yet and
any number chosen now would be invented.

**Discharged by the plan's E10-11**, once E10-3 produces the first real
measurement — not "workstream A", which was the earlier and wrong reference.
**"Do not adopt floors" is a valid outcome** provided the measured figures are
recorded here; leaving this open indefinitely is not.

### 13.5 Deferred, with a reason

**Versioning the `KINDLY_DIAG` frame format.** There is no version field today, so
adding one is a wire-format change rather than a test decision, and the two
endpoints ship in the same wheel and cannot disagree by version in practice. If a
version field is ever added, §4.3 gains an unknown-version case; until then there
is nothing to test.

---

## 14. Open gaps

- **`.system_design/SYSTEM_DESIGN.md` does not exist.** This document describes
  testing a system whose "To Be" design was never written down, so component
  boundaries are inferred from code. §13.1 is the first thing that design should
  settle.
- **No per-task requirements artefacts of any kind.** The repository contains
  neither a root `REQUIREMENTS.md` nor a `.requirements/` directory, so §6's
  acceptance scenarios are reverse-engineered from tool docstrings rather than
  derived from stated acceptance criteria. Which convention should apply is a
  separate question from this document; the gap is the same either way.
- **`nodriver` and Chromium version drift** is untested at any layer. The worker
  carries compatibility shims (`_patch_nodriver_network_encoding`,
  `_is_snap_browser`), which implies breakage has happened and will recur.
- **`_split_worker_diagnostics` is dead code** (§4.3), flagged for removal under
  a separate change.
