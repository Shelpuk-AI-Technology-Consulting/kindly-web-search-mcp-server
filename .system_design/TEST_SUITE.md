# Test suite design — Kindly Web Search MCP Server

**Status:** To Be. Describes the target test suite, not the one that exists today.
**Last verified against the codebase:** 2026-08-30.

---

## 1. Why this document exists

The suite has 27 test files (~3,857 lines) against ~6,849 lines of source, and no
stated strategy behind them. There is no `[tool.pytest.ini_options]` block, no
registered markers, no CI at all (`.github/` does not exist), no coverage
measurement, and two different environment gates for live tests.

That would be ordinary technical debt. The reason to write this down now is what
the suite is actively hiding.

### 1.1 The suite is permanently red, and the redness is stale tests

A full run reports **11 failed, 169 passed, 3 skipped**. That "11 failed" has been
treated as the expected baseline. It is not an environment quirk — all 11 are
tests that no longer match the code they test, and they fail on every platform:

| Tests | Failure | Actual cause |
|---|---|---|
| 7 in `test_nodriver_worker_sandbox.py` | `TypeError: _fetch_html() missing 5 required keyword-only arguments: 'reuse_browser', 'remote_host', 'remote_port', 'user_data_dir', 'overall_timeout_seconds'` | `_fetch_html` grew five required arguments; the callers in the tests were never updated |
| 3 in `test_universal_html_loader.py` | `AttributeError: '_FakeProc' object has no attribute 'stdout'` | `fetch_html_via_nodriver` now streams `proc.stdout`; the test's fake process never grew one |
| 1 in `test_server.py` | `AssertionError: 3 != 1` | patches `server.os.name` to assert a Windows concurrency cap that was removed from `_resolve_web_search_max_concurrency` |

The consequence is worse than eleven red lines. Every one of these tests targets
`scrape/` — 2,969 lines covering subprocess spawning, Chromium lifecycle, stream
framing, timeouts and process-tree termination, which is the riskiest code in the
repository. Each fails while *constructing* its fixture, before reaching a single
assertion. **`scrape/` therefore has no effective test coverage at all**, and
`scrape/chromium_pool.py` (372 lines) has no test file in the first place.

A suite whose normal state is red teaches everyone to stop reading the number.
Restoring a green baseline is the precondition for everything else in this
document; it is Section 7, and nothing else matters until it is done.

### 1.2 What is already good

Three things are worth preserving verbatim, because they are unusually strong and
this document should not disturb them:

- `test_provider_registry_consistency.py` holds `PROVIDERS` in sync with the
  README, `.env.example` and the `web_search` tool docstring. It exists because
  two providers were once added without updating all five copies, and a
  SerpBase-only install was told no provider was configured while search worked.
- `test_dependency_constraints.py` guards the dependency bounds that actually
  reach users, because the documented `uvx --from git+…` install re-resolves from
  PyPI on every start and ignores `uv.lock`.
- `test_tool_descriptions.py` asserts the tool docstrings stay agent-oriented.

All three are contract tests in the sense used below. They are the model for
Section 4.

---

## 2. The layer model, applied to this system

Four layers. Each proves a different claim, runs at a different speed, and fails
for a different reason. The point of the split is that a failure tells you where
to look.

```
L4  Product      MCP client ─► server ─► provider ─► resolver ─► Chromium ─► Markdown
                 proves:  a real MCP client gets usable Markdown back
                 covers:  real MCP session (stdio + Streamable HTTP) · gated live
                          provider smoke · extraction quality on live pages
                 speed:   seconds–minutes · a handful · nightly

L3  Subsystem    [server + uvicorn + transport security]   [loader + real Chromium]
                 proves:  one component and its real infrastructure wire up
                 covers:  transport/CORS/rebinding over a real socket · worker
                          subprocess lifecycle · ChromiumPool slots and shutdown
                 speed:   ~100ms–seconds · dozens · per PR, Linux container

L2  Contract     MCP tool schema ⇄ clients · parent ⇄ worker protocol ·
                 registry ⇄ README/.env.example · declared deps ⇄ imports
                 proves:  two independently-versioned parties still agree
                 speed:   fast and hermetic, like a unit test

L1  Component    URL parsers · Markdown transforms · env resolvers ·
                 diagnostics redaction · text accumulators
                 proves:  a unit's logic is right for every input that matters
                 speed:   milliseconds · hundreds · the base of everything
```

### 2.1 The allocation rule

**Prove each behaviour at the lowest layer that can prove it.**

| Claim | Layer | Why not higher |
|---|---|---|
| `FASTMCP_ALLOWED_HOSTS=" a , ,b "` parses to `["a","b"]` | L1 | A pure string function needs no server |
| A `SERPER_API_KEY` never appears in diagnostics output | L1 | It is a property of one function, `mask_env_values` |
| Renaming `num_results` breaks every MCP client | L2 | Reading the generated schema is enough; no session needed |
| A killed worker leaves no orphaned Chromium | L3 | Needs a real process tree; nothing smaller can prove it |
| A client can call `web_search` and read Markdown | L4 | The claim *is* the whole path |

Two consequences, both of which the current suite violates somewhere:

1. **Do not re-prove a lower-layer claim higher up.** An L4 session test asserts
   the journey completes. It does not re-check that `num_results` clamps to 5 —
   L1 owns that.
2. **When a bug escapes, add the test at the lowest layer that would have caught
   it.** Add a higher-layer test only when the *wiring* was at fault. The PR #50
   SSE regression is the worked example: the bug was choosing the wrong ASGI app,
   so the right home was a fast L1-style assertion on the selection, plus one L3
   test that `/sse` really answers.

---

## 3. L1 — Component and property tests

This is the bulk of the suite and where the repo has the most to gain, because
most of its risky logic is already pure or one small refactor away from it.

### 3.1 Targets

**URL parsers.** `parse_stackexchange_url`, `parse_github_issue_url`,
`parse_github_discussion_url`, `parse_wikipedia_url`, `parse_arxiv_url`.

These deserve property-based testing rather than more examples, because of how
`resolve_page_content_markdown` uses them. Routing in `content/resolver.py` is a
first-match chain: each parser is tried in order and the first that does not raise
wins. So the load-bearing property is not about any single parser:

> **No URL may be accepted by two parsers.**

An overlap is a silent mis-route — the URL goes to whichever handler happens to
be earlier in the chain, and nothing anywhere reports a problem. That is exactly
the class of bug example-based tests miss and Hypothesis finds.

Secondary properties, one per parser: a URL it accepts round-trips to the same
identifiers; a URL it rejects raises its own error type and never a bare
`Exception`.

**Diagnostics redaction.** `redact_url_credentials`, `mask_env_values`,
`truncate_text`, `sample_data`, `_apply_line_limit`.

Security-critical, and stated as an invariant rather than a set of examples:

> **No value of a secret-named variable, and no URL userinfo component, ever
> survives into diagnostics output.**

`mask_env_values` applies two different rules — whole-value masking for
secret-named keys, userinfo-only redaction for everything else, so that
`HTTP_PROXY` stays debuggable. A property test generates both kinds and asserts
the secret substring is absent from the result. `test_diagnostics_masking.py`
already covers the examples; the property is what stops a new key-naming pattern
from slipping through.

**Environment resolvers.** `_resolve_transport`, `_resolve_host_port`,
`_resolve_tool_total_timeout_seconds`, `_resolve_web_search_max_concurrency`,
`_resolve_transport_security`, `_cors_origin_regex` in `server.py`; the
`_resolve_*` family and `_parse_port_range` in `chromium_pool.py` and
`nodriver_worker.py`.

Table-driven `parametrize`, covering unset, blank, valid, malformed and
out-of-range for each. This is where malformed operator input silently becomes a
wrong default — the failure mode PR #50 hit twice.

**Markdown transforms.** `html_to_markdown`, `sanitize_markdown`,
`extract_content_as_markdown`, `_apply_markdown_cap`.

These run against a **checked-in corpus of saved HTML** under
`tests/corpus/html/`, not against live pages. This is the most important
structural move in this document: extraction is the thing most people would reach
for an end-to-end browser test to check, and doing so makes it slow,
network-dependent and non-reproducible. Saving the HTML once turns almost all of
it into deterministic millisecond-scale L1. Live extraction still gets tested, but
only at L4 and only for what the corpus structurally cannot show — that real sites
have not changed shape.

**Text accumulators.** `_append_tail_text`, `_split_worker_diagnostics`, and the
encoding-cookie helpers in `nodriver_worker.py`. Pure functions over strings and
byte lists; cheap to cover exhaustively, and currently covered by nothing that
runs.

### 3.2 Validation by mutation testing

Line coverage proves a line ran, not that a test would fail if it broke. For the
modules above — where correctness is the whole point — the check is mutation
testing: `mutmut` mutates the source and reports which mutants the suite fails to
kill.

Scope it deliberately to the pure-logic modules (`utils/diagnostics.py`,
`content/*_url` parsers, the `_resolve_*` families). Running it across `scrape/`
would spend hours mutating subprocess plumbing that L1 does not own.

**Constraint, confirmed against mutmut's documentation:** mutmut requires a system
with `fork()` support, so on Windows it runs only under WSL. Since this repo is
developed on Windows, mutation runs belong in Linux CI (Section 8), never in the
local edit-test loop. Treat surviving mutants as a review queue, not a gate.

---

## 4. L2 — Contract tests

A contract test pins an agreement between two parties that version independently.
It is as fast and hermetic as a unit test; what makes it L2 is what it protects.

### 4.1 Keep as-is

`test_provider_registry_consistency.py` and `test_dependency_constraints.py`, for
the reasons in Section 1.2.

### 4.2 New: the MCP tool schema

The JSON Schema that FastMCP generates from the `web_search` and `get_content`
signatures is the public API of this server. Every MCP client binds to it.
Renaming a parameter, changing a type, or making an optional argument required is
a breaking change for every user — and nothing currently notices.

The schema is reachable in-process:

```python
tools = await mcp.list_tools()
# web_search -> properties {'num_results', 'query'}, required ['query']
# get_content -> properties {'url'},                 required ['url']
```

Pin it as a golden file under `tests/golden/tool_schema.json`. A diff forces the
author to confirm the break is intended and to say so in the release notes,
rather than discovering it from a user's broken client.

### 4.3 New: the parent ⇄ worker protocol

`nodriver_worker.py` runs as a spawned subprocess and reports back to
`universal_html.py` by framing lines on stderr:

```
KINDLY_DIAG {"stage": "...", "message": "...", ...}
```

`_split_worker_diagnostics` parses those out, keeps non-matching lines as human
stderr, and caps malformed payloads at three samples. Two independently-editable
sides, a wire format between them, and no test pinning the format — which is
precisely the seam that rotted (Section 1.1).

The contract test asserts the format in both directions from fixed strings: a
well-formed line parses to the expected entry, an unprefixed line survives as
stderr text, a malformed payload is sampled and does not raise, and the sample cap
holds. No subprocess required, so this stays fast and hermetic; the *spawning* is
L3's problem.

### 4.4 New: response-model invariants

The `web_search` docstring and `models.py` both promise `page_content` is
**always a string**, including on every failure path — that is what lets clients
skip null handling. `resolve_page_content_markdown` can return `None`, and both
tools convert it to a deterministic Markdown note. Assert the promise directly
over each failure branch: timeout, handler exception, unsupported type.

### 4.5 Extend: undeclared imports

`tests/test_searxng_unit.py` imports `anyio`, which is declared nowhere — it
arrives transitively via `httpx`/`mcp`. This is the same pattern the dependency
guard was built to catch, and the same one that PR #50 fixed for `starlette` and
`uvicorn`. Extend `DIRECTLY_IMPORTED_DEPENDENCIES` to cover the `dev` extra and
add `anyio` to it.

---

## 5. L3 — Subsystem tests against real infrastructure

The "real infrastructure" in this system is a real socket, a real subprocess and a
real Chromium. These tests are slower and Linux-only in CI; there should be dozens
of them, not hundreds.

### 5.1 Server over a real socket

Start the server on a port and drive it with `httpx`. Covers what no in-process
test can: that transport selection, DNS-rebinding protection and the CORS policy
agree once real headers are involved.

The probe written by hand during the PR #50 review becomes a permanent test here.
Its cases are the specification:

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

### 5.2 Worker subprocess lifecycle

Spawn, stream stdout and stderr, heartbeat, timeout, and process-tree
termination — `fetch_html_via_nodriver` and `_terminate_process_tree`.

Rewritten from scratch rather than repaired. The existing tests fake the process
object, and faking it is what let them drift out of sync with the real one for
five arguments and a stream. Using a real short-lived child process (a small
Python script that prints known frames and optionally hangs) removes the fake, and
with it the drift.

Load-bearing cases: a clean run returns Markdown and parsed diagnostics; a hanging
child is killed at the deadline; a killed parent leaves no orphaned child; a child
writing garbage to stderr does not crash the parent.

### 5.3 ChromiumPool

Currently untested. Slot acquisition and release, reuse when enabled
(`KINDLY_NODRIVER_REUSE_BROWSER`), pool sizing
(`KINDLY_NODRIVER_BROWSER_POOL_SIZE`), port allocation inside
`KINDLY_NODRIVER_PORT_RANGE`, acquire timeout under contention
(`KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS`), concurrent acquisition, and shutdown
leaving no live browsers.

---

## 6. L4 — Product tests

Few, slow, and gated. Their job is to catch what the layers below structurally
cannot: that the whole path works and that the outside world has not moved.

**Real MCP session.** Using the SDK's own client, run `initialize` → `tools/list`
→ `tools/call` over stdio and over Streamable HTTP, with a stubbed provider so the
result is deterministic. This proves the server is a valid MCP server, which is
the one claim no lower layer makes.

**Live provider smoke.** One real query per configured provider, asserting shape
and non-emptiness — never exact content.

**Live extraction quality.** `get_content` against a small fixed URL list,
thresholded rather than exact-matched: content is non-empty, exceeds a minimum
length, contains an expected anchor phrase, and is not the deterministic failure
note. Real pages change; an exact-match assertion here would be a flake generator.
This is the honest analogue of an LLM eval — the output is not reproducible, so
the assertion is a quality bar, not an equality.

**One gate, not two.** Live tests are currently split across
`KINDLY_RUN_LIVE_TESTS` (`test_live_fetch_urls.py`) and `RUN_LIVE_TESTS`
(`test_serper_live.py`). Standardize on **`KINDLY_RUN_LIVE_TESTS=1`**, matching
the project's `KINDLY_` prefix convention, and mark them `@pytest.mark.live`.

---

## 7. Restoring the baseline

**This is the first workstream and it blocks the rest.** Nothing in Sections 3–6
is worth building on top of a suite nobody trusts.

1. **Rewrite the 7 `test_nodriver_worker_sandbox.py` tests** against the real
   `_fetch_html` signature, as L3 tests with a real child process (Section 5.2).
2. **Rewrite the 3 `test_universal_html_loader.py` tests** the same way.
3. **Delete `test_web_search_concurrency_defaults_on_windows`.** It asserts a
   Windows concurrency cap that no longer exists in the code. Deleting is correct
   here — the test does not encode a requirement anyone still wants; keeping a
   green version of it would enshrine behaviour that was deliberately removed.
4. **Make green the gate.** Once the suite is green, wire the CI in Section 8 so
   it cannot go red unnoticed again.

Steps 1 and 2 are the reason the rot happened, so they carry the design rule that
prevents a recurrence: **stop faking the subprocess.** A hand-written fake of an
object you do not own drifts silently; a real child process cannot.

---

## 8. Tooling, configuration and CI

### 8.1 Test dependencies

Versions verified 2026-08-30.

| Tool | Version | Purpose | Note |
|---|---|---|---|
| `pytest` | 9.x | Runner | Already declared |
| `pytest-asyncio` | 1.4.x | Async tests | `asyncio_mode = "auto"`; the repo is pure asyncio, so the per-test marker is pure noise |
| `hypothesis` | ≥ 6.167 | Properties in §3.1 | Latest release 6.167.0, 2026-08-30 |
| `coverage` | current | Signal only | See §8.2 |
| `mutmut` | 3.x | L1 validation | **Needs `fork()` — WSL or Linux CI only** |
| `ruff` | current | Lint | Already declared, not currently installed |
| `packaging` | current | Dependency guard | Already declared |

**No HTTP-mocking library.** `httpx.MockTransport` is already the pattern in
`tests/test_searxng_unit.py`, it is built into a dependency the project already
has, and it is sufficient for every case here. Adding `respx` would buy
convenience at the cost of a dependency — and this repo has been bitten twice by
dependencies it did not control.

### 8.2 On coverage

Coverage is reported, not gated. **No line-coverage target is set**, deliberately:
a percentage target rewards executing lines, and the suite's actual problem was
never unexecuted lines — it was eleven tests that executed plenty of lines while
asserting nothing. Where correctness matters, mutation testing (§3.2) is the
measure. Coverage is used for one thing: spotting whole modules nobody tests,
which is how `chromium_pool.py` should have been noticed.

### 8.3 First pytest configuration

The repo has no `[tool.pytest.ini_options]` at all. Add one:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers"
asyncio_mode = "auto"
markers = [
    "live: hits the real network; needs KINDLY_RUN_LIVE_TESTS=1",
    "subsystem: needs real infrastructure (browser, subprocess); Linux CI",
    "slow: over a second",
]
```

`--strict-markers` matters: without it a typo in a marker name silently produces a
test that no selector ever runs.

### 8.4 CI

There is no CI. This is the first pipeline:

| Trigger | Runs | Platform |
|---|---|---|
| every push | L1 + L2 — hermetic, no browser, no network | Windows **and** Linux |
| every PR | + L3 (`-m subsystem`) with real Chromium | Linux container |
| nightly | + L4 live (`-m live`), extraction evals, mutation run | Linux |

Windows is in the per-push matrix because it is the primary development platform
and three of the four content loaders spawn subprocesses, where Windows and POSIX
differ most. Linux is there because it is what the Docker image ships.

**Green on every push is a hard gate.** That is the entire point of this document.

---

## 9. Design decisions worth recording

Choices a future reader might otherwise mistake for accidents.

**Saved-HTML corpus instead of live extraction tests (§3.1).** Extraction is the
most tempting thing to test end-to-end and the worst thing to test end-to-end.
Pages change, so assertions either weaken to uselessness or flake. Saving the HTML
fixes the input and moves ~90% of extraction testing to L1. The cost is that the
corpus goes stale relative to real sites, which is exactly what the thresholded
L4 checks in Section 6 exist to catch.

**Real child process instead of a fake in L3 (§5.2, §7).** Directly contradicts
the usual "don't spawn processes in tests" advice, and does so on evidence: the
fake drifted from the real object and hid the drift for ten tests. A real process
is slower and Linux-gated, and it cannot silently disagree with reality.

**Mutation testing over a coverage target (§3.2, §8.2).** Unusual to state a
project has *no* coverage threshold. Stated explicitly so nobody adds one thinking
it was an oversight. The suite's failure mode was assertion quality, which a
coverage percentage cannot see and mutation testing measures directly.

**Deleting a failing test rather than fixing it (§7 step 3).** Normally the wrong
instinct. It is right here because the test encodes a requirement that was
deliberately removed; "fixing" it would restore a behaviour nobody wants, and
leaving it red is what created this document.

---

## 10. Open gaps

- **`.system_design/SYSTEM_DESIGN.md` does not exist.** This document describes how
  to test a system whose "To Be" design has never been written down, so its
  component boundaries are inferred from the code rather than traced to a design.
  That inversion should be closed.
- **No `REQUIREMENTS.md` history.** `.requirements/` does not exist, so the L4
  acceptance scenarios in Section 6 have no stated acceptance criteria to derive
  from and are reverse-engineered from the tool docstrings instead.
- **`nodriver` and Chromium version drift** is untested at any layer. The worker
  already carries compatibility shims (`_patch_nodriver_network_encoding`,
  `_is_snap_browser`), which implies breakage has happened before and will again.
