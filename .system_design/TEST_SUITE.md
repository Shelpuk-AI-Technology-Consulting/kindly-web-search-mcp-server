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

**The count is platform-dependent and Windows reports the smaller number.**
`test_nodriver_worker_sandbox.py` contains **eight** stale `_fetch_html` callers.
The eighth, `test_forces_sandbox_off_when_running_as_root`, skips on Windows
because `os.geteuid` does not exist there (`test_nodriver_worker_sandbox.py:199`),
so POSIX should show **12** failures. *That POSIX figure is read from source, not
executed;* §8 makes measuring it a task. Baselines are quoted per platform, from
real runs, or not quoted.

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
`monkeypatch.setattr(os, "geteuid", …)` for identity, with the `hasattr` branch
covered by deleting the attribute. `test_worker_launch_args_redaction.py` is the
working precedent for the style.

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
directory is cleaned up with `ignore_cleanup_errors`. These use narrow doubles
around `_fetch_html`, built from the real interface (autospec) so a signature
change fails loudly.

**Autospec for callables, a Protocol for the process object.** Autospec validates
call signatures, which is exactly what the eight stale tests needed and did not
have. It does *not* declare instance attributes: `create_autospec` on
`asyncio.subprocess.Process` gives no `stdout`, `stderr`, `stdin` or `pid`
(§8A step 3), so the process double is pinned by a Protocol instead. Use each for
the failure it actually catches.

These complement the L1 flag tests of §3.1 — a fixture child cannot assert which
Chromium flags were chosen, and a resolver test cannot assert a process tree
died.

### 5.3 Chromium-specific — Linux container

`ChromiumPool`: slot acquisition and release, reuse
(`KINDLY_NODRIVER_REUSE_BROWSER`), pool sizing
(`KINDLY_NODRIVER_BROWSER_POOL_SIZE`), port allocation within
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention
(`KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS`), concurrent acquisition, shutdown.
Plus one real fetch through `_fetch_html` against a locally served page.

### 5.4 Anti-flake and cleanup requirements

Every test in §5.2 and §5.3 must:

- Use a **readiness handshake**, never a sleep — wait for a known line or an
  accepting port, with a timeout.
- Allocate **ephemeral ports** and **isolated profile directories** per test.
- Carry a **per-test timeout** shorter than the job timeout.
- Clean up in `finally`, keyed on **the PIDs this test spawned**. "No live
  browsers" means those children and their descendants are gone — never a scan
  for processes named `chrome`, which is vulnerable to PID reuse and would kill a
  developer's own browser.
- Capture and attach child stdout/stderr on failure.

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
  checkout**. Running from the repo root puts `src/` on `sys.path` ahead of the
  installed package on some invocations.
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

### 7.2 Outbound request policy — undefined

The server is unauthenticated and `get_content` fetches **any URL the caller
supplies, from wherever the server runs**. Nothing currently restricts:

- Non-HTTP schemes — `file:`, `data:`, `ftp:`, `chrome:`.
- Loopback, RFC1918, link-local, IPv6 unique-local, and cloud metadata addresses
  (`169.254.169.254`).
- Redirects that begin public and land private.
- DNS rebinding between validation and connection (TOCTOU).
- Proxy interaction, where `KINDLY_CHROME_PROXY` may reach networks the host
  cannot.

**The policy is a product decision** (§13). Once stated, tests follow at three
boundaries and the shape is the same either way:

1. **URL validation** — L1, table-driven over scheme and address class.
2. **Redirect handling** — L3, a local server issuing a public→private redirect.
3. **Connection** — L3, a hostname whose resolution changes between validation
   and connect.

---

## 8. Workstreams, in order

**A. Restore green.** Blocks everything else, and is done with *minimal* edits —
no framework migration mixed in, so a failure means a stale test, not a
conversion bug.

1. Measure the baseline on Linux; record both platforms.
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
   fake implement it. A type checker then flags a missing attribute, and a small
   conformance test asserts the fake satisfies the Protocol at runtime. Keep the
   real-child contract test of §5.2 as well: the Protocol pins the shape,
   the real child pins the behaviour, and neither substitutes for the other.
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
| Worker retry/termination orchestration | gap — stale | L3 portable | `subsystem` | |
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
| `coverage` | `>=7,<8` | Branch coverage and the committed baseline (§10.4) |
| `diff-cover` | `>=10.4,<11` | Diff coverage on changed lines; 10.4 is the floor because `--branch-coverage` does not exist below it (§10.4) |
| `mutmut` | `>=3,<4` | L1 validation; **needs `fork()` — Linux/WSL only** |
| `ruff` | `>=0.6,<1` | Lint |
| `packaging` | `>=24` | Dependency guard |

Bounds, not "current": this project has twice been broken by an unbounded
dependency, which is why `test_dependency_constraints.py` exists. **Update
policy:** bounds are raised in a PR that runs the full suite against the new
version; Dependabot may propose, never auto-merge.

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
| `ci-required` | aggregation only — no tests | — | Linux |

`fast`, `fast-extras`, `subsystem`, `chromium` and `package` run on every push
and PR.

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
  needs: [fast, fast-extras, subsystem, chromium, package]
  runs-on: ubuntu-latest
  steps:
    - name: Fail unless every dependency succeeded
      if: >-
        needs.fast.result != 'success' ||
        needs['fast-extras'].result != 'success' ||
        needs.subsystem.result != 'success' ||
        needs.chromium.result != 'success' ||
        needs.package.result != 'success'
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
    if minimum and len(session.items) < minimum:
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
`subsystem`, `chromium` and `package` in its `needs`.

### 10.4 Coverage

No absolute percentage target: a percentage rewards executing lines and can be
satisfied by deleting tests. Two enforced controls, plus reporting.

**1. Diff coverage — the PR gate.** `diff-cover --fail-under=80` over the changed
lines of each PR. This is the control that does the work: unambiguous, no
historical comparison needed, and it puts the requirement where a reviewer can
act on it.

The promise is precise: **at least 80% of coverable changed lines must be
covered** — not "new code is covered or the PR is red". A PR can legitimately land
with an uncovered changed line.

Operationally:

- The combined-coverage job (below) emits `coverage.xml`; `diff-cover` consumes
  that, not the console report.
- `--compare-branch=origin/main`.
- `actions/checkout` defaults to a shallow clone, where `origin/main` does not
  exist and `diff-cover` cannot compute a diff. The job sets `fetch-depth: 0`, or
  fetches `main` explicitly before running.
- **The gate measures changed-line execution only.** A changed line where only one
  branch was taken counts as covered here. Branch completeness is measured by the
  whole-project report (`branch = true`) and by mutation testing (§3.2), not by
  this gate.

  Branch-aware diff coverage needs `diff-cover >= 10.4` and its `--branch-coverage`
  flag; the constraint in §10.2 allows that version so the option is open, but the
  flag is **not** assumed here and must be exercised before anyone claims the gate
  enforces branch completeness. An earlier draft of this document asserted branch
  behaviour the pinned version could not deliver — the gate is only ever as strong
  as the flag actually passed.

**2. A committed baseline, ratcheted.** Total branch coverage is written to
`coverage-baseline.json`, committed. Three checks, and all three are needed —
any one alone leaves a hole:

1. `head_baseline >= base_baseline`, where the base value is read from the PR's
   **base SHA as supplied by the event** — `github.event.pull_request.base.sha` —
   via `git show <base-sha>:coverage-baseline.json`. Not `origin/main`, whose tip
   can move while the workflow is running, which would make the comparison depend
   on unrelated merges. This is the ratchet: lowering the committed number in the
   same PR that lowers coverage now **fails CI**, rather than merely being
   visible in review.
2. `measured_head == head_baseline` to the documented precision. Without this, a
   coverage *rise* need never be recorded, and a later PR could silently give the
   gain back while still clearing the stale floor.
3. `measured_head >= head_baseline` is implied by (2) but stated separately so a
   precision mismatch reports as such rather than as a regression.

**Bootstrap and non-PR cases.** Check (1) needs a value that does not exist on the
first run, and there is no base at all outside a PR:

- **Bootstrap.** When `coverage-baseline.json` is absent from the base SHA, skip
  check (1) and require only (2). This is a one-time state; once the file is on
  `main` the check is live. Do not treat "absent" as zero — a missing file would
  otherwise let any value through as an improvement.
- **Pushes to `main`.** Run checks (2) and (3) only. There is no base to ratchet
  against, and the value was already gated on the PR that merged it.
- **PRs targeting a branch other than `main`.** The rule is unchanged, because it
  reads the event's base SHA rather than a hard-coded branch. A stacked PR
  therefore ratchets against its own parent, which is the intended behaviour.

Read the number from `coverage json` output, not by parsing the formatted console
report — the text report is a presentation format and its rounding is not a
contract.

**Precision.** Two decimal places on the percentage, taken from the JSON totals.

This is deliberately a stored-and-compared baseline rather than a re-measured
merge base. Re-measuring means checking out, installing, running and combining
three revisions per side — six runs — and raises a question with no good answer:
does the base revision run *its* tests or *head's*? Each measures something
different and neither is what the control is for. Comparing two committed numbers
gives a true non-decreasing guarantee at the cost of one `git show`.

There is **no tolerance band.** A tolerance turns a ratchet into a
bounded-regression policy that compounds: twenty PRs each losing 0.09 points lose
nearly two points with every job green.

**Exact equality requires a pinned environment, not merely a fixed job set.** A
fixed *selection* of jobs is not a fixed *execution environment*, and the
measurement set as first written was not reproducible: it included the moving
"newest allowed" `mcp` release, unpinned application dependencies, broadly bounded
test tooling, and a Chromium job whose browser package changes underneath it. Any
of those can move the percentage on an unchanged commit, which under exact
equality means unrelated PRs fail or authors are pushed into meaningless baseline
edits — and a baseline people routinely edit to make CI pass is no longer a
control.

The ratchet therefore runs in a **pinned lane**:

- Dependencies installed from the pinned `requirements.txt` (`pip freeze` output,
  `mcp==1.25.0` among them), not from the `pyproject.toml` ranges.
- An exact `mcp` version, never the "newest allowed" axis.
- The Chromium container referenced **by image digest**, not by tag.
- Exact `==` pins for `coverage` and the test tooling in the ratchet lane, since a
  coverage-measurement change alters the number without any code changing.
- Python 3.13 only.

**Compatibility jobs do not feed the ratchet.** The `mcp` {min, max} axis, Python
3.14, and `fast-extras` exist to catch breakage across the supported range. Their
coverage artefacts are discarded. Mixing them in is what made exact equality
impossible, and their purpose — does it still work — is served by them passing,
not by their coverage number.

Bumping a pin in the ratchet lane is a deliberate PR that may legitimately move
the baseline; that is a reviewable event, which is exactly the property the whole
control is for.

**Measurement definition.** `coverage combine` over the **pinned-lane** runs of
`fast`, `subsystem` and `chromium` on Linux — one fixed set in one fixed
environment, so a platform-conditional branch cannot inflate the number in one job
and fail in another. Windows and every compatibility axis are reported but
excluded.
`branch = true`. `[paths]` in `.coveragerc` maps Windows and Linux checkouts to a
single root so combined data does not double-count. Exclusions: `tests/` itself,
`if TYPE_CHECKING:` blocks and `__main__` guards — nothing else, and in
particular no blanket exclusion of `scrape/`.

**What coverage would and would not have caught.** The stale tests raise
`TypeError` before `_fetch_html`'s body runs, so a coverage *report* would show
that function dark to anyone who looked. The baseline check is weaker: losing
eleven tests' worth of lines may or may not move the whole-project total enough
to trip it. Coverage is a diagnostic that would have made this visible on
inspection; the baseline is what makes silent erosion fail; mutation testing
(§3.2) is what judges assertion quality. None substitutes for another.

### 10.5 pytest configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers"
asyncio_mode = "auto"
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

## 13. Open decisions

Four items need an answer from the maintainer; none can be settled by this
document.

1. **Outbound request policy (§7.2).** Is fetching private-network addresses
   intentional? It plausibly is — self-hosted SearXNG and internal documentation
   are exactly what this server is pointed at, and a blanket RFC1918 block would
   break those users. The tests in §7.2 apply either way; what is not acceptable
   is leaving it undefined on an unauthenticated server.
2. **Tool result schemas (§11.3).** Annotate the tools with their response models
   so clients receive an `outputSchema`, or keep `-> dict` and pin its absence?
   This is an API change with client impact.
3. **Owners in the risk matrix (§9).** Assigning maintainers is not this
   document's call; the column is otherwise complete.
4. **Per-module coverage minimums.** Diff coverage and the committed baseline are adopted
   (§10.4); fixed per-module floors are not, because no coverage measurement
   exists in this repo yet and any number chosen now would be invented. Revisit
   once workstream A produces a measured baseline.

Deferred with a reason: **versioning the `KINDLY_DIAG` frame format.** There is
no version field today, so adding one is a wire-format change, not a test
decision, and the two endpoints ship in the same wheel and cannot disagree by
version in practice. If a version field is ever added, §4.3 gains an
unknown-version case; until then there is nothing to test.

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
