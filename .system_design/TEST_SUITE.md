# Test suite design — Kindly Web Search MCP Server

**Status:** To Be. Describes the target test suite, not the one that exists today.
**Revision:** 2 — revised after review (see §13 for what was deferred and why), and
extended to use the `SERPER_API_KEY` Actions secret now configured on the
repository (§6, §10.3).

**Measurement environment.** Every number below was measured on:
Windows 10 Pro 19045 · CPython 3.13 · `mcp` 1.25.0 · `starlette` 0.50.0 ·
`uvicorn` 0.40.0 · `pytest` 9.1.1 · repo at `b3581ca`. Results that were *derived
from source rather than executed* are labelled as such. Reproducing the baseline
on Linux is a task in §8, not an assumption in §1.

---

## 1. Why this document exists

The suite has 27 test files (~3,857 lines) against ~6,849 lines of source, and no
stated strategy behind them. There is no `[tool.pytest.ini_options]` block, no
registered markers, no CI at all (`.github/` does not exist), no coverage
measurement, and two different environment gates for live tests.

That would be ordinary technical debt. The reason to write this down now is that
the suite has a permanently red baseline that everyone has learned to read past.

### 1.1 The baseline, measured

On the environment above: **11 failed, 169 passed, 3 skipped, 9 subtests passed.**

| Tests | Failure | Cause |
|---|---|---|
| 7 of 10 in `test_nodriver_worker_sandbox.py` | `TypeError: _fetch_html() missing 5 required keyword-only arguments: 'reuse_browser', 'remote_host', 'remote_port', 'user_data_dir', 'overall_timeout_seconds'` | `_fetch_html` grew five required arguments; the callers in the tests were never updated |
| 3 of 18 in `test_universal_html_loader.py` | `AttributeError: '_FakeProc' object has no attribute 'stdout'` | `fetch_html_via_nodriver` now streams `proc.stdout`; the test's fake process never grew one |
| 1 in `test_server.py` | `AssertionError: 3 != 1` | patches `server.os.name` to assert a Windows concurrency cap that was removed from `_resolve_web_search_max_concurrency` |

**The count is platform-dependent, and Windows reports the smaller number.**
`test_nodriver_worker_sandbox.py` contains **eight** stale `_fetch_html` callers,
not seven. The eighth, `test_forces_sandbox_off_when_running_as_root`, skips on
Windows because `os.geteuid` does not exist there
(`test_nodriver_worker_sandbox.py:199`). On POSIX it should fail like the rest,
giving **12 failures rather than 11**. *That POSIX figure is derived from reading
the source; it has not been executed.* Measuring it is a task in §8.

The lesson for this document is the general one: a per-OS baseline must be
recorded from real runs on each supported platform before it is quoted anywhere.

### 1.2 What is actually uncovered

Eleven red tests do not mean `scrape/` is untested. Most of the package's tests
pass and assert real behaviour. The uncovered surface is specific:

**Uncovered — the worker lifecycle, both sides of the process boundary:**

- `nodriver_worker._fetch_html` (~480 lines) and the helpers it drives:
  `_launch_chromium`, `_wait_for_devtools_ready`, `_terminate_process`, and the
  connect-retry loop. All 8 stale sandbox tests target this entry point.
- `universal_html.fetch_html_via_nodriver` and the parent-side streaming path:
  `_read_stdout_stream`, `_read_stderr_stream`, `_consume_stderr_line`,
  `_emit_worker_heartbeat`, `_terminate_process_tree`. The 3 stale loader tests
  target this.
- **All of `scrape/chromium_pool.py`** (372 lines) — no test file references it.

**Covered and working, not to be disturbed:**

- 15 of 18 tests in `test_universal_html_loader.py` cover the Markdown-suffix
  probe path end to end — `_build_md_suffix_url`, `_probe_markdown_suffix`,
  `_probe_markdown_accept_blanket`, allowlist behaviour, oversize capping, error
  swallowing, and `load_url_as_markdown` routing including the PDF skip.
- `test_worker_launch_args_redaction.py` covers proxy-credential redaction in
  `_build_chromium_launch_args`, testing that **pure function directly**. This is
  the pattern §3.1 and §8 ask the repaired sandbox tests to follow.

**Not a `scrape/` problem at all:** the concurrency failure is in `server.py`.
Revision 1 of this document folded it into a claim about `scrape/`; that was
wrong.

### 1.3 What is already good

Three things are worth preserving verbatim:

- `test_provider_registry_consistency.py` holds `PROVIDERS` in sync with the
  README, `.env.example` and the `web_search` tool docstring. It exists because
  two providers were once added without updating all five copies, and a
  SerpBase-only install was told no provider was configured while search worked.
- `test_dependency_constraints.py` guards the dependency bounds that reach users,
  because the documented `uvx --from git+…` install re-resolves from PyPI on every
  start and ignores `uv.lock`.
- `test_tool_descriptions.py` asserts the tool docstrings stay agent-oriented.

---

## 2. The layer model, applied to this system

```
L4  Product      MCP client ─► server ─► provider ─► resolver ─► Chromium ─► Markdown
                 proves:  a real client gets a correct outcome from a real install
                 covers:  MCP session from an installed wheel · live provider and
                          extraction canaries
                 speed:   seconds–minutes · a handful

L3  Subsystem    [server + uvicorn]  [worker + real process]  [pool + real Chromium]
                 proves:  one component and its real infrastructure wire up
                 covers:  transport/CORS over a real socket · process lifecycle,
                          timeout and cleanup · ChromiumPool slots and shutdown
                 speed:   ~100ms–seconds · dozens

L2  Contract     MCP tool schema ⇄ clients · parent ⇄ worker frame format ·
                 registry ⇄ README/.env.example · declared deps ⇄ imports
                 proves:  two parties that change separately still agree
                 speed:   fast and hermetic, like a unit test

L1  Component    URL parsers · Markdown transforms · env resolvers · launch-arg
                 builders · diagnostics redaction · text accumulators
                 proves:  a unit's logic is right for every input that matters
                 speed:   milliseconds · hundreds · the base of everything
```

### 2.1 The allocation rule

**Prove each behaviour at the lowest layer that can prove it.**

| Claim | Layer | Why not higher |
|---|---|---|
| `FASTMCP_ALLOWED_HOSTS=" a , ,b "` parses to `["a","b"]` | L1 | A pure string function needs no server |
| `--no-sandbox` is passed when sandbox is disabled | L1 | `_build_chromium_launch_args` is pure; going through `_fetch_html` is what broke |
| Renaming `num_results` breaks every MCP client | L2 | Reading the generated schema is enough |
| A killed worker leaves no orphaned Chromium | L3 | Needs a real process tree |
| A client can install the wheel and call `web_search` | L4 | The claim *is* the whole path |

**The corollary that this codebase got wrong.** Eight sandbox tests assert
worker-*internal* decisions — sandbox flags, root handling, executable discovery,
retry counts, profile cleanup — by driving `_fetch_html`, an ~480-line coroutine
that launches a browser. Those assertions are about pure functions
(`_build_chromium_launch_args`, `_resolve_sandbox_enabled`,
`_resolve_browser_executable_path`, `_resolve_start_retry_attempts`). Routing them
through the largest function in the module is why a signature change five
arguments wide silently disabled all of them. **Testing above the layer that owns
the claim is what created the outage.**

---

## 3. L1 — Component and property tests

### 3.1 Targets

**Launch-argument and sandbox decisions.** `_build_chromium_launch_args`,
`_resolve_sandbox_enabled`, `_resolve_browser_executable_path`,
`_resolve_start_retry_attempts`, `_resolve_snap_backoff_multiplier`,
`_is_snap_browser`, `_is_retryable_browser_connect_error`.

These are pure and already have a working precedent:
`test_worker_launch_args_redaction.py` tests `_build_chromium_launch_args`
directly and passes. The repaired sandbox assertions belong here, in that style —
not behind `_fetch_html`.

**URL parsers.** `parse_stackexchange_url`, `parse_github_issue_url`,
`parse_github_discussion_url`, `parse_wikipedia_url`, `parse_arxiv_url`.

`resolve_page_content_markdown` tries these in order and takes the first that does
not raise, so the load-bearing property is not about any single parser:

> **No URL may be accepted by two parsers.**

An overlap is a silent mis-route with no error anywhere. Hypothesis finds this;
examples do not. Secondary per-parser properties: an accepted URL round-trips to
the same identifiers; a rejected URL raises that parser's own error type, never a
bare `Exception`.

**Environment resolvers.** The `_resolve_*` families in `server.py`,
`chromium_pool.py` and `nodriver_worker.py`, plus `_parse_port_range`,
`_resolve_transport_security` and `_cors_origin_regex`. Table-driven
`parametrize` over unset, blank, valid, malformed, zero, negative and
out-of-range. This is where malformed operator input silently becomes a wrong
default.

**Markdown transforms.** `html_to_markdown`, `sanitize_markdown`,
`extract_content_as_markdown`, `_apply_markdown_cap`, `_build_md_suffix_url`,
against the corpus governed by §3.3. This converts most extraction testing from
non-reproducible live checks into deterministic millisecond tests.

**Text accumulators.** `_append_tail_text` and the encoding-cookie helpers in
`nodriver_worker.py`. Pure, cheap to cover exhaustively, currently covered by
nothing that runs.

### 3.2 Validation by mutation testing

Line coverage proves a line ran, not that a test would fail if it broke. For the
modules above, `mutmut` is the measure: it mutates the source and reports which
mutants survive.

Scope it to the pure-logic modules (`utils/diagnostics.py`, the `*_url` parsers,
the `_resolve_*` and launch-arg families). Mutating `scrape/` subprocess plumbing
would burn hours on code L1 does not own.

**Constraint, confirmed against mutmut's documentation:** mutmut requires `fork()`
support, so on Windows it runs only under WSL. Since this repo is developed on
Windows, mutation runs live in Linux CI, never in the local edit-test loop.
Surviving mutants are a review queue, not a gate.

### 3.3 HTML corpus governance

The corpus lives in `tests/corpus/html/` and is the input to every extraction
test. Without rules it rots into a liability, so:

- **Two tiers.** Small handcrafted fragments for individual transformations
  (a table, a code block, nested lists, an entity edge case), plus a limited set
  of sanitized real-page snapshots for whole-document behaviour. Handcrafted
  fixtures carry the bulk of the assertions; snapshots exist to catch structural
  surprises.
- **Provenance and licensing.** Each snapshot has a sidecar `.meta.json` naming
  the source URL, capture date, and licence or rationale for inclusion. Do not
  snapshot pages whose terms forbid redistribution; prefer permissively licensed
  or project-owned pages.
- **Sanitization before commit.** Strip cookies, tokens, session identifiers,
  personal data, analytics and third-party script bodies. A snapshot is a
  committed artefact, so treat it as published.
- **Size cap.** 200 KB per snapshot; trim to the region under test. Anything
  larger is a sign the fixture should be a handcrafted fragment instead.
- **Assertion style.** Structural assertions by default (headings preserved, code
  fences intact, no raw HTML left, length within the cap). Golden-file matching
  only for the small handcrafted fragments, where a diff is readable.
- **Refresh.** Snapshots are refreshed only when a live canary (§6) fails and
  shows the real page changed shape. Refresh is a reviewed commit that states
  what changed, never an automatic overwrite.

---

## 4. L2 — Contract tests

### 4.1 Keep as-is

The three in §1.3.

### 4.2 The MCP tool schema

The JSON Schema FastMCP generates from the `web_search` and `get_content`
signatures is this server's public API. Renaming a parameter or changing a type
breaks every client, and nothing currently notices. It is reachable in-process:

```python
tools = await mcp.list_tools()
# web_search -> properties {'num_results', 'query'}, required ['query']
# get_content -> properties {'url'},                 required ['url']
```

A naive golden file is wrong in both directions at once: it churns whenever an
allowed SDK release (`mcp>=1.25,<2`) reorders keys or rewrites a generated
description, and it pins only inputs while saying nothing about results. So:

- **Normalize before comparing.** Sort keys, drop generated `title` fields, and
  compare descriptions by presence rather than exact text.
- **Assert semantics, not bytes.** Parameter names, JSON types, required-ness,
  defaults, the declared result shape (`WebSearchResponse`, `GetContentResponse`)
  and the error representation.
- **Run against both ends of the supported range** — the minimum (`1.25.0`) and
  the newest release the specifier allows — so a schema change introduced by an
  SDK upgrade fails here rather than at a user.

### 4.3 The parent ⇄ worker frame format

`nodriver_worker.py` reports to `universal_html.py` by framing lines on stderr:

```
KINDLY_DIAG {"stage": "...", "msg": "...", ...}
```

**Test the live path, not the dead one.** `_split_worker_diagnostics`
(`universal_html.py:123`) parses a whole captured stderr string and **has no
callers anywhere in `src/` or `tests/`** — the production path streams instead,
through `_read_stderr_stream` → `_consume_stderr_line`. A contract test aimed at
`_split_worker_diagnostics` would exercise dead code. (That function is also a
dead-code removal candidate; flagged here, not deleted by this document.)

Because the two sides ship from the same wheel this is an **internal protocol**,
not an agreement between independently versioned parties. It still earns a
contract test: the two sides are edited independently, and the format is the only
thing holding them together.

Extract the frame encoder and decoder into one place and test both directions:

- Emission through the real encoder produces a line the real decoder accepts.
- Streaming consumption handles fragmentation: a frame split across chunk
  boundaries, several frames in one chunk, CRLF endings, EOF with no trailing
  newline, and a multi-byte character split across chunks.
- Malformed payloads are sampled and capped without raising, and non-`KINDLY_DIAG`
  lines survive as human-readable stderr.
- An oversized line is truncated rather than buffered without limit.

**This contract would not have prevented the current outage.** The stale
five-argument calls and the missing `_FakeProc.stdout` are unrelated to the frame
format. It is worth having on its own merits; revision 1 credited it with a
prevention it does not deliver.

### 4.4 Response-model invariants

`page_content` is promised to be **always a string** by both the tool docstring
and `models.py`, on every failure path — that promise is what lets clients skip
null handling. `resolve_page_content_markdown` can return `None`, and both tools
convert it. Assert the promise across each branch: timeout, handler exception,
unsupported type, empty provider result.

### 4.5 Import/declaration agreement

`tests/test_searxng_unit.py` imports `anyio`, which is declared nowhere. **The fix
is to remove the import, not to declare the dependency** — see §10.1; migrating to
pytest-native async removes every `anyio.run` wrapper in the suite. The
declaration guard should then assert that test-only imports stay within the
declared `dev` extra, so the next undeclared import is caught rather than
retro-fitted.

---

## 5. L3 — Subsystem tests against real infrastructure

Dozens of tests, not hundreds. Split by what infrastructure they actually need,
because that determines where they can run (§10.3).

### 5.1 Server over a real socket — portable

Start the server on an ephemeral port and drive it with `httpx`. Runs on Windows
and Linux; needs no browser.

| Configuration | Request | Expected |
|---|---|---|
| default | `POST /mcp`, `Origin: https://evil.example` | `403` |
| default | `POST /mcp`, non-loopback `Host` | `421` |
| default | preflight from a foreign origin | no `Access-Control-Allow-Origin` |
| default | preflight + `POST` from `http://localhost:3000` | `200`, origin echoed |
| `FASTMCP_ALLOWED_HOSTS` set | `POST /mcp` by service name | `200` |
| `FASTMCP_ALLOWED_HOSTS` set | loopback origin | `200` |
| `FASTMCP_ALLOWED_HOSTS` set | foreign origin | `403` |
| `--sse` | `GET /sse` / `GET /mcp` | stream held open / `404` |

### 5.2 Worker process lifecycle — portable

Spawn, stream, heartbeat, timeout and process-tree termination, using a **real
child process**: a small fixture script that emits known frames on demand and can
be told to hang. No Chromium, so this runs on both OSes — which is the point,
since process handling is where Windows and POSIX differ most.

Cases: a clean run returns the child's **HTML** output and parsed diagnostics
(`fetch_html_via_nodriver` returns rendered HTML — Markdown conversion happens
later, in `load_url_as_markdown`); a hanging child is killed at the deadline; a
killed parent leaves no orphaned child; a child writing garbage to stderr does not
crash the parent; a child exiting non-zero surfaces a readable error.

These tests **complement** the L1 argument tests in §3.1 rather than replacing
them. A generic fixture child cannot assert which Chromium flags were chosen; a
pure function test cannot assert that a process tree died. Both are needed.

### 5.3 Chromium-specific — Linux container

`ChromiumPool`: slot acquisition and release, reuse when enabled
(`KINDLY_NODRIVER_REUSE_BROWSER`), pool sizing
(`KINDLY_NODRIVER_BROWSER_POOL_SIZE`), port allocation within
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention
(`KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS`), concurrent acquisition, and shutdown.
Plus one real end-to-end fetch through `_fetch_html` against a locally served
page.

### 5.4 Anti-flake and cleanup requirements

Tests that deliberately hang or kill process trees are the likeliest source of
flake and of leaked resources. Every test in §5.2 and §5.3 must:

- Use a **readiness handshake**, never a sleep — wait for a known line on stdout
  or a port to accept a connection, with a timeout.
- Allocate **ephemeral ports** and **isolated profile directories** per test; no
  fixed ports, no shared user-data dir.
- Carry a **per-test timeout** shorter than the job timeout, so a hang fails
  legibly instead of stalling CI.
- Clean up in `finally`, keyed on **the PIDs this test spawned**. "No live
  browsers" means those specific children and their descendants are gone —
  never a scan for processes named `chrome`, which is vulnerable to PID reuse and
  would kill a developer's own browser.
- Capture and attach child stdout/stderr on failure; a dead child with no output
  is undiagnosable.

---

## 6. L4 — Product tests

**Deterministic MCP session — every PR, not nightly.** Build the wheel, install
it, and drive the real console entrypoint (`kindly-web-search-mcp-server
start-mcp-server` and `mcp-web-search --http`) with the SDK's own client:
`initialize` → `tools/list` → `tools/call`, with a stubbed provider. Because it is
deterministic it belongs in the per-PR gate, and running it from an installed
wheel also covers the packaging and entrypoint path that nothing else touches.

Being stubbed, it proves transport, packaging and protocol conformance — **not**
the provider → resolver → Chromium path. That is a separate, separately named
canary:

**Live canaries — nightly, gated.** One real query per configured provider
(shape and non-emptiness only, never exact content), and `get_content` against a
small fixed URL list with thresholded assertions: non-empty, above a minimum
length, contains an expected anchor phrase, and is not the deterministic failure
note. Real pages change, so an equality assertion here is a flake generator. A
canary failure is the trigger to refresh the corpus (§3.3).

**One gate, not two.** Live tests are split today across `KINDLY_RUN_LIVE_TESTS`
(`test_live_fetch_urls.py`) and `RUN_LIVE_TESTS` (`test_serper_live.py`).
Standardize on **`KINDLY_RUN_LIVE_TESTS=1`** plus `@pytest.mark.live`.

**A skipped live suite is a failed live suite.** A nightly job where every test
skips for want of a credential is green and worthless. Therefore: one CI matrix
job per intended provider and capability; each **enabled** job **fails** if its
required secret is absent rather than skipping; the run reports skip counts; and a
named owner is alerted when the scheduled workflow fails (§10.3).

**Credentials available today.** `SERPER_API_KEY` exists as a GitHub Actions
secret. That is enough to enable two of the live jobs immediately:

- **`live-serper`** — the provider canary for Serper, which is also the default
  and first-priority provider in `PROVIDERS`, so it exercises the routing path
  most installs take. Enabled now; fails if the secret is missing.
- **`live-extraction`** — the `get_content` quality canary. This needs a browser
  but **no provider credential at all**, because it fetches a URL directly
  without going through search. It can be enabled now regardless of secrets, on
  the Linux Chromium image.

SerpBase, Tavily, SearXNG and Sofya stay **out of the matrix** until their
credentials exist, rather than sitting in it failing every night. Adding a
credential is what adds the job — that keeps "a red nightly means something
broke" true from day one. The matrix should be data-driven off the provider list
so that adding a secret is the only step required.

---

## 7. Security testing

### 7.1 Diagnostics must be sanitized at the boundary

Revision 1 stated the invariant *no secret survives into diagnostics* but only
proposed testing `mask_env_values`. That proves a helper, not the invariant.

`Diagnostics.emit` and `emit_diagnostic` (`utils/diagnostics.py:133,151`) apply
**no redaction at all** — only JSON serialization and a line-length cap. Callers
pass raw data: `server.py` emits `{"url": url}` and `{"detail": full_detail}`
where the detail is unfiltered exception text. So `get_content("https://user:token@host/x")`
places the credential verbatim in diagnostics.

This is an egress path, not merely a logging one: `Diagnostics.entries` is
returned to the caller in `GetContentResponse.diagnostics` and
`WebSearchResult.diagnostics`. Unredacted values go back over the MCP wire.

**Design requirement:** sanitize inside the serialization boundary, so every
emission is covered regardless of caller discipline. Test the **emitted JSON**
end to end, not the helper.

The policy must state, and tests must cover:

- URL userinfo (`user:pass@`), in both raw and percent-encoded form.
- Credential-bearing query parameters (`?token=`, `?api_key=`, `?sig=`) — name
  list, and what happens to an unrecognized parameter name.
- Request and response headers, notably `Authorization`, `Cookie`, `Set-Cookie`.
- Exception messages and tracebacks, which routinely embed the failing URL.
- Page-content samples (`sample_data`), which can contain anything.
- Low-entropy secret values, where substring matching produces false positives —
  the policy should say whether it redacts by key name, by pattern, or both.

### 7.2 Outbound request policy — undefined, untested

The server is unauthenticated and `get_content` fetches **any URL the caller
supplies, from wherever the server runs**. Revision 1 covered inbound protections
(DNS rebinding, CORS) and said nothing about outbound. That is the larger half of
the exposure and the one PR #50 flagged as the reason inbound defaults mattered.

Nothing in the code currently restricts:

- Non-HTTP schemes — `file:`, `data:`, `ftp:`, `chrome:`.
- Loopback, RFC1918, link-local, IPv6 unique-local, and cloud metadata addresses
  (`169.254.169.254`).
- Redirects that begin public and land private.
- DNS rebinding between validation and connection (TOCTOU).
- Proxy interaction, where `KINDLY_CHROME_PROXY` may reach networks the host
  cannot.

**This document cannot decide the policy** — see §13. Private-network fetching may
well be intentional, since a self-hosted SearXNG and internal documentation are
plausible targets. What is not acceptable is that it is neither stated nor tested.

Once the policy is stated, tests follow at three boundaries, and the shape is the
same whichever way the decision goes:

1. **URL validation** — L1, table-driven over scheme and address class.
2. **Redirect handling** — L3, a local server issuing a public→private redirect.
3. **Connection** — L3, a hostname whose resolution changes between validation
   and connect.

---

## 8. Restoring the baseline

**This is the first workstream and it blocks the rest.**

1. **Measure the baseline on Linux** and record both platforms' numbers. The
   POSIX figure in §1.1 is derived from source, not executed, and the design
   should not rest on an unrun number.
2. **Repair the 8 stale sandbox tests at the right layer.** Their assertions —
   sandbox flags, root handling, executable discovery, retry counts, profile
   cleanup — belong as L1 tests against `_build_chromium_launch_args`,
   `_resolve_sandbox_enabled`, `_resolve_browser_executable_path` and
   `_resolve_start_retry_attempts`, in the style of the passing
   `test_worker_launch_args_redaction.py`. Where an assertion genuinely needs the
   coroutine, use an autospecced double so a signature change fails loudly
   instead of silently. Do **not** replace them with a real-process test: a
   generic child cannot assert which flags Chromium received.
3. **Repair the 3 stale loader tests.** They assert command arguments and
   environment propagation, which a real child process also cannot verify. Fix
   the fake by building it from the real `asyncio.subprocess.Process` interface —
   a shared fixture builder or an autospecced double — so it cannot drift again.
4. **Rewrite the concurrency test OS-neutral; do not delete it.** Only the
   Windows-specific expected default is obsolete. The same method still covers
   explicit values, malformed input, zero and negative — real requirements with no
   other home. Replace it with one parameterized test over defaulting,
   validation, clamping and `num_results` limiting, with the `os.name` patching
   removed.
5. **Add the real-process lifecycle tests of §5.2** as new coverage, alongside the
   repaired tests rather than instead of them.
6. **Make green enforceable** — §10.3.

---

## 9. Risk-to-test matrix

Revision 1 specified parsers, diagnostics and Chromium in detail and left most of
the product unaddressed. Every source subsystem and public behaviour gets a row,
a layer and a CI job. **Owner is deliberately blank** — see §13.

| Subsystem / behaviour | Today | Target layer | CI job | Owner |
|---|---|---|---|---|
| Provider routing, strict order, no fallback | good (`test_search_router.py`) | L1 | push | |
| Serper / SerpBase / Tavily / SearXNG / Sofya parsing | partial — unit tests exist per provider | L1 | push | |
| Provider errors: 401, 429, malformed JSON, timeout, empty | **gap** | L1 (`httpx.MockTransport`) | push | |
| Provider registry ⇄ docs | good | L2 | push | |
| StackExchange / GitHub issues / GitHub discussions / Wikipedia loaders | partial — parsing covered, failure paths thin | L1 + L2 | push | |
| arXiv + PDF extraction | partial | L1 | push | |
| Optional `pdf-advanced` extras present *and* absent | **gap** | L2 | push (both installs) | |
| Resolver routing and per-handler fallback | partial | L1 | push | |
| Markdown transforms | partial | L1 + corpus | push | |
| URL parser mutual exclusivity | **gap** | L1 property | push | |
| Env resolvers across all three modules | partial | L1 | push | |
| Diagnostics redaction at the emit boundary | **gap** (§7.1) | L1 + L2 | push | |
| Outbound URL policy | **gap, undefined** (§7.2) | L1 + L3 | push + PR | |
| Inbound transport security, CORS, SSE | good (PR #50) | L1 + L3 | push + PR | |
| MCP tool schema stability | **gap** | L2 | push | |
| Parent ⇄ worker frame format | **gap** | L2 | push | |
| Worker process lifecycle and cleanup | **gap — stale** | L3 portable | PR | |
| ChromiumPool | **gap — no tests** | L3 Chromium | PR | |
| CLI entrypoints and `--` forwarding | good (`test_uvx_cli.py`) | L1 | push | |
| Wheel build, install, console entrypoints | **gap** | L4 | PR | |
| Documented `uvx --from git+…` path | **gap** | L4 | nightly | |
| Dependency bounds | good | L2 | push | |
| Tool-call cancellation and partial results | **gap** | L3 | PR | |
| Output size limits and truncation | partial | L1 | push | |
| Live Serper reachability | gated | L4 | `live-serper` (secret available) | |
| Live SerpBase / Tavily / SearXNG / Sofya | gated | L4 | not enabled — no credential yet | |
| Live extraction quality | gated | L4 | `live-extraction` (no secret needed) | |

---

## 10. Tooling, configuration and CI

### 10.1 Async direction — pick one

The suite currently mixes `unittest.IsolatedAsyncioTestCase` with hand-rolled
`anyio.run` wrappers. Adding `pytest-asyncio` on top of both would make three.

**Standardize on `pytest-asyncio` with `asyncio_mode = "auto"`.** The project is
pure asyncio, so the marker is noise. Migration removes every `anyio.run` wrapper
and every `IsolatedAsyncioTestCase`, which is what lets `test_searxng_unit.py`
stop importing `anyio` (§4.5) rather than the project declaring a dependency it
does not otherwise want.

### 10.2 Dependencies and version policy

| Tool | Constraint | Purpose |
|---|---|---|
| `pytest` | `>=9,<10` | Runner |
| `pytest-asyncio` | `>=1.4,<2` | Async tests, `asyncio_mode = "auto"` |
| `hypothesis` | `>=6.167,<7` | Properties in §3.1 |
| `coverage` | `>=7,<8` | Signal and diff gate (§10.4) |
| `mutmut` | `>=3,<4` | L1 validation; **needs `fork()` — Linux/WSL only** |
| `ruff` | `>=0.6,<1` | Lint |
| `packaging` | `>=24` | Dependency guard |

Bounds, not "current". Upper bounds exist because this project has twice been
broken by an unbounded dependency, which is why `test_dependency_constraints.py`
exists. **Update policy:** bounds are raised deliberately in a PR that runs the
full suite against the new version; Dependabot may propose, never auto-merge.
Where a runtime dependency has a supported range — `mcp>=1.25,<2` — CI tests the
**minimum and the newest allowed**, since testing only one end leaves the other
unverified for users.

**No HTTP-mocking library.** `httpx.MockTransport` is already the pattern in
`test_searxng_unit.py:57`, is built into a dependency the project already has, and
covers every case here.

### 10.3 CI

There is no CI. This is the first pipeline.

| Job | Selection | Platform |
|---|---|---|
| `fast` | `-m "not live and not subsystem and not chromium"` | Windows **and** Linux |
| `subsystem` | `-m "subsystem and not chromium"` | Windows **and** Linux |
| `chromium` | `-m chromium` | Linux container |
| `package` | wheel build + install + §6 deterministic session | Linux |
| `live-serper` (nightly) | `-m "live and serper"`, needs `SERPER_API_KEY` | Linux |
| `live-extraction` (nightly) | `-m "live and extraction"`, needs a browser, no secret | Linux container |
| `mutation` (nightly) | `mutmut run` over the §3.2 scope | Linux |

`fast`, `subsystem`, `chromium` and `package` run on every push and PR.

**Secrets in CI.** `SERPER_API_KEY` is configured as a repository Actions secret,
which is what enables `live-serper`. Two rules govern its use:

- **Never expose it to untrusted code.** Secrets are not available to
  `pull_request` runs from forks, and that is the correct default — PR #50 came
  from a fork. The live jobs run on `schedule` and `workflow_dispatch` against
  `main`, never on fork PRs. Do not "fix" a fork PR's missing secret by switching
  to `pull_request_target`; that would run untrusted code with the key in scope.
- **Fail loudly when it is missing on an enabled job.** `live-serper` asserts the
  secret is present before collecting tests, so a rotated-and-not-updated key
  produces a red nightly rather than a green skip.

The key is billable. Keep the nightly canary to one query per provider (§6);
breadth belongs in the mocked L1 provider tests, which cost nothing.

**The Windows rationale now matches the allocation.** Revision 1 justified
Windows by subprocess divergence while placing every subprocess test in a
Linux-only tier, so Windows ran only tests with no subprocesses. Splitting L3
into portable (§5.2) and Chromium-specific (§5.3) is what makes the claim true:
the portable process-lifecycle tests run on both platforms, which is where
`_terminate_process_tree` and `create_subprocess_exec` actually differ.

**Marker expressions must be explicit.** Marking a test does not exclude it from a
plain `pytest` run. Each job names its `-m` expression, and `addopts` carries
`--strict-markers` so a typo'd marker fails instead of silently matching nothing.

**"Green" is only a gate once it is enforced.** A passing workflow is not a gate
until `fast`, `subsystem`, `chromium` and `package` are **required checks** under
branch protection on `main`. Configuring that is part of this workstream, not an
afterthought.

### 10.4 On coverage

Revision 1 set no coverage control at all and justified it with an argument that
does not hold: it claimed the stale tests "executed plenty of lines while
asserting nothing". They did not — a `TypeError` on a missing keyword argument
raises *before* `_fetch_html` runs, so coverage would have shown that function
going dark. Coverage would have caught this.

The real objection is only to an absolute percentage target, which rewards
executing lines and can be satisfied by deleting tests. The controls are
therefore:

- **Branch coverage, reported on every run.**
- **A no-regression ratchet:** total coverage may not fall between commits. This
  is what stops a suite from going green by deletion, which `--strict-markers` and
  a passing run do not.
- **Diff coverage on changed lines** for every PR, which puts the requirement
  where a reviewer can act on it.
- **Mutation testing (§3.2)** for the modules where correctness is the product.

No absolute per-module minimum is set — see §13.

### 10.5 First pytest configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers"
asyncio_mode = "auto"
markers = [
    "live: hits the real network; needs KINDLY_RUN_LIVE_TESTS=1",
    "subsystem: needs a real socket or child process; portable",
    "chromium: needs a real browser; Linux container only",
    "serper: live test requiring SERPER_API_KEY",
    "extraction: live content-extraction canary; browser but no credential",
    "slow: over a second",
]
```

`serper` and `extraction` are registered because §10.3 selects on them
(`-m "live and serper"`). Under `--strict-markers` an unregistered marker is an
error, which is the behaviour we want — a job selecting a marker that nobody
applied would otherwise run zero tests and pass.

---

## 11. Design decisions worth recording

**Assertions live at the layer that owns them (§2.1, §8).** The eight sandbox
tests are the cautionary example: worker-internal flag decisions asserted through
an ~480-line browser-launching coroutine, disabled wholesale by one signature
change. Pure decisions get pure tests.

**Real child process *and* narrow doubles at L3 (§5.2, §8).** Revision 1 said
"stop faking the subprocess" and proposed replacing every stale test with a real
process. That was wrong: a generic fixture child cannot assert which Chromium
flags or environment a call produced. The two are complementary — doubles for
"what did we ask for", real processes for "what actually happened to the process
tree". Note that a fixture child can itself drift from the real worker; it is a
weaker guarantee than revision 1 claimed, not an absolute one.

**Saved-HTML corpus instead of live extraction tests (§3.1, §3.3).** Extraction is
the most tempting thing to test end-to-end and the worst — pages change, so
assertions either weaken to uselessness or flake. Fixing the input moves most of
it to L1. The cost is corpus staleness, which the §6 canaries exist to detect.

**Sanitization at the serialization boundary, not at call sites (§7.1).** Relying
on every caller to redact is a policy that fails the first time someone adds an
`emit`. One choke point is testable end to end.

**No absolute coverage target, but a ratchet and diff coverage (§10.4).** Stated
explicitly, with revision 1's incorrect rationale corrected, so nobody reinstates
a percentage thinking the omission was an oversight.

---

## 12. Open gaps

- **`.system_design/SYSTEM_DESIGN.md` does not exist.** This document describes how
  to test a system whose "To Be" design was never written down, so component
  boundaries are inferred from code rather than traced to a design. That inversion
  should be closed, and §7.2's outbound policy is the first thing it should settle.
- **No per-task requirements history.** There is no `.requirements/` directory, so
  the §6 acceptance scenarios have no stated acceptance criteria to derive from and
  are reverse-engineered from the tool docstrings.
- **`nodriver` and Chromium version drift** is untested at any layer. The worker
  already carries compatibility shims (`_patch_nodriver_network_encoding`,
  `_is_snap_browser`), which implies breakage has happened and will recur.
- **`_split_worker_diagnostics` is dead code** (§4.3). Flagged for removal under a
  separate change, not deleted here.

---

## 13. Comments on deferred review feedback

Revision 2 applied the review in full except for the four items below. Each is
deferred for a stated reason rather than declined.

**1. Versioning the `KINDLY_DIAG` frame format.** The review asked the parent ⇄
worker contract test to cover "protocol-version handling". There is no version
field in the format today, so this is a proposal to change the wire format, not a
test-design decision — and adding one has real cost for a protocol whose two
endpoints ship in the same wheel and can never disagree by version in practice.
**Deferred to `SYSTEM_DESIGN.md`.** If a version field is added, §4.3 gains a case
for an unknown version; until then there is nothing to test. Every other item in
that review point — real encoder/decoder, fragmentation, CRLF, EOF without
newline, Unicode split across chunks, oversized lines, malformed frames — is
applied.

**2. Absolute per-module coverage minimums.** The review suggested "minimums for
critical modules" alongside the no-regression ratchet and diff coverage. The
ratchet and diff coverage are adopted (§10.4); fixed per-module floors are not.
Any number chosen today would be invented, since there is no coverage measurement
in this repo at all and the baseline is unknown. A floor set above the true value
blocks the first PR; set below it, it is decoration. **Revisit once §8 produces a
measured baseline**, at which point a floor can be set from data rather than
guessed.

**3. Owners in the risk-to-test matrix.** The review asked for an owner per row.
The column exists in §9 and is intentionally empty: assigning maintainers is not a
call this document can make. **Needs the maintainer to fill in** — the matrix is
otherwise complete and usable without it.

**4. The outbound request policy itself.** §7.2 adopts the review's finding in
full and specifies the tests, but deliberately does not decide whether fetching
private-network addresses is permitted. It is plausibly intentional — a
self-hosted SearXNG instance and internal documentation are exactly the sort of
thing this server is pointed at, and a blanket RFC1918 block would break those
users. **This is a product decision and needs an explicit answer**, after which
§7.2's three test boundaries apply unchanged in either direction. What is not
acceptable is leaving it undefined, which is the state today.

One correction the review itself needs: it states that
`test_nodriver_worker_sandbox.py` produces "seven failures on Windows but eight on
POSIX". The Windows figure is confirmed by measurement; the POSIX figure is
consistent with the source but has not been executed here, and §8 step 1 makes
measuring it a task rather than an assumption.
