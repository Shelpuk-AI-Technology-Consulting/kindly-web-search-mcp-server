# Code Review Instructions

You are reviewing a pull request in **Kindly Web Search MCP Server**, an MCP
server that gives a coding agent web search plus robust retrieval of a page's
content as LLM-ready Markdown. Judge the change against this repository's own
documents and stated behaviour, not against generic best practice.

## Before reviewing: read the specification

Do not review the diff in isolation.

**Always, in full:**

- `README.md` — **this is the specification.** There is no separate design
  document in this repository. The README carries the tool contract, the
  client-by-client setup for seven MCP clients, the transport and allowlist
  behaviour, the proxy configuration, and the troubleshooting that tells a user
  what a `403` or `421` means. It is ~38 KB; read it whole.

> This is a deliberate departure from the upstream repository this review system
> was adopted from, which reads three `.system_design/` documents and navigates a
> fourth by section. **This repository has no `.system_design/` directory.** The
> README is what stands in its place, and it is small enough to read in full. Do
> not treat its absence as a gap you should work around by inferring a design —
> and do not report "there is no design document" as a finding on every pull
> request. It is recorded here.

**Additionally, based on what the pull request touches:**

| If the PR changes | Also read |
|---|---|
| anything with a per-task requirements document | `.requirements/<datetime>_<feature>/REQUIREMENTS.md` — the **As Is / To Be / Requirements / Acceptance Criteria** are the traceability target |
| `pyproject.toml`, `requirements.txt`, `Dockerfile` | `.env.example`, and the dependency comments in `pyproject.toml` itself — several bounds are load-bearing and say why |
| the tools, transports, or allowlists | the matching README sections, and `SECURITY.md` (see the caveat below) |
| anything under `.github/` | the rule file `ci.md`, which carries this workflow's own invariants |
| a design document, if one has since been added | that document, plus any module `.system_design/` |

`SECURITY.md` is, as committed, the **unmodified GitHub template** — placeholder
prose and an invented version table naming releases this project has never had.
Do not read it as this project's threat model, and do not raise its placeholder
state as a new finding on every pull request; it is a known, pre-existing gap.

Per-task requirements live in `.requirements/<datetime>_<feature>/REQUIREMENTS.md`
when the author created one. Where there is none, the README and the pull request
description are the statement of intent.

## What this product is

A coding agent — Claude Code, Codex, Cursor, Copilot — needs current information:
the exact text of an error, an API signature that changed last month, the version
a package is actually on. This server gives it two tools. `web_search` runs a
query through whichever of five providers the user configured and returns results
**with the page content already fetched**. `get_content` takes a URL it is
already holding and returns that page as Markdown, through a site-specific
handler where one exists (Stack Exchange, GitHub issues and discussions,
Wikipedia, arXiv) and through a headless-Chromium fallback where none does.

The deliverable is **text that goes straight into another model's context
window**. Rules that follow from that, and that a change must never weaken:

- **The retrieved content is the product.** Anything that degrades it — a
  truncated page, a silently dropped result, a handler that falls through to a
  worse path without saying so — is a product defect, not a technical one.
- **Silence is the failure mode to hunt for.** Every failure path in this
  repository returns a Markdown note rather than raising, because the caller is a
  model and `page_content` is documented as always a string. That is correct, and
  it means a broken provider, a dead handler or an empty extraction can look
  exactly like a page with little on it. A change that adds a path where nothing
  is returned and nothing is recorded is a critical finding.
- **The user's API keys must never leave the request they authenticate.** Not
  into a log, an exception message, a diagnostics payload, or a subprocess
  command line. `utils/diagnostics.py` is the single definition of what a
  credential looks like; a second definition anywhere is itself the defect.
- **Everything fetched is untrusted.** Search results, page bodies, forum posts,
  a self-hosted SearXNG instance's response — all of it is attacker-influenceable
  and all of it ends up in a model's context. Widening what is rendered widens
  that surface.
- **The server runs on the user's own machine, usually on loopback.** DNS-rebinding
  protection and the host and origin allowlists are what stop a web page the user
  is visiting from driving their local server. They stay on.
- **stdout belongs to the JSON-RPC stream.** Under the stdio transport, anything
  printed to stdout corrupts the protocol and breaks the client's session.

## Highest-priority checks

### Requirement and design conformance

- Trace the change to something stated: an acceptance criterion in a task's
  `REQUIREMENTS.md`, a documented behaviour in the README, or the pull request's
  own description. Flag behaviour that matches none of them.
- Flag any change that contradicts what the README says. **Quote both sides.**
- A change to the tool signatures or descriptions, the response models, the
  transports, the allowlists, or any environment variable must be accompanied by
  the matching README and `.env.example` update. Code that drifts from the
  documentation is a finding.
- The tool docstrings in `server.py` are **sent to the calling model** as the
  tool specification. Judge an edit to one as a prompt change, not a comment
  change.

### Tests

- Production code with no corresponding test is a finding.
- Ask whether each test would actually fail if the behaviour it names were
  broken. A test that passes either way is not a test.
- Tests live in `tests/`, one module per unit under review. The review system's
  own tests live in `.github/review/tests/`.
- Several tests exist specifically to hold an invariant nothing else enforces —
  `test_dependency_constraints.py`, `test_provider_registry_consistency.py`,
  `test_tool_descriptions.py`, `test_diagnostics_masking.py`,
  `test_worker_launch_args_redaction.py`. A change that weakens one of these to
  make a diff pass is a critical finding.

### Documentation

- **Every class, method, and function should carry a Google-style docstring**
  with `Args:`, `Returns:` and `Raises:`, and every module a module-level
  docstring. Hold **changed** symbols to that; mention pre-existing gaps without
  making them blockers.
- A docstring that no longer matches the code above which it sits is worse than
  none.

## Standard checks

- **Correctness.** Off-by-one, empty and `None` collections, wrong defaults,
  boundary conditions. For each non-trivial branch, ask what happens when the
  input is empty, missing, malformed, or very large — and remember that "very
  large" here means a hostile page.
- **Concurrency and resource lifecycle.** This server is `async` throughout and
  keeps a pool of real browser processes. Look for an acquire without a matching
  release on the exception path, a leaked process or port, an unbounded `gather`,
  a retry loop that can outlive the caller's timeout budget, and state that
  survives between requests to different sites.
- **Security.** Hard-coded secrets, credentials in logs or command lines, missing
  validation at trust boundaries, a URL from an untrusted source formatted into a
  request path, a Chromium flag that disables isolation.
- **Error handling.** A bare `except Exception` in the resolver chain is
  deliberate — the contract is that no handler failure escapes to the caller — so
  do not flag it as sloppy; do check that what it returns carries only the
  exception's *type*, never its message. Elsewhere, look for swallowed errors,
  errors re-raised as the wrong type across a boundary, and retry behaviour that
  amplifies a failure rather than containing it.
- **Backward compatibility.** The response models are the wire format for every
  MCP client and there is no version negotiation. Field renames, removals and
  type changes break clients silently. So do changes to the four
  `[project.scripts]` entry-point names, which users have pasted into their
  client configuration.

## Scope discipline

Every changed line should trace to the stated purpose of the pull request.

- Flag unrelated refactors, formatting churn, and improvements to adjacent code.
- Flag new abstractions, configuration options, or flexibility that was not asked
  for.
- Removing imports or helpers that the change itself orphaned is correct.
  Deleting pre-existing dead code is out of scope — mention it, do not require
  it.

## Severity: how to rank what you found

This decides the order findings are reported in, not how many to report. Sweep
every dimension, then rank.

1. **Critical** — a requirement implemented incorrectly or not at all; an
   acceptance criterion with no test that proves it; a credential that can reach
   a log, a message or a command line; a weakened allowlist, sandbox or
   redaction; a data-corrupting bug; broken backward compatibility of a response
   model or an entry point; a leaked process or wedged pool; anything that
   weakens the product rules above.
2. **Warning** — likely defects under specific conditions, weak error handling,
   missing edge-case tests, code that has drifted from the README or
   `.env.example`.
3. **Suggestion** — readability, naming, mild duplication, a comment that would
   save a future reader.

Explain the failure mode in one or two sentences and give a concrete fix.

## Do not

- **Do not run tests, linters, builds, or dependency installs.** Read-only
  inspection — `grep`, `find`, `wc`, `awk`, `git diff`, `git log`, `git show` —
  is not only allowed but expected; verify a count rather than assuming one.

  ⚠️ Note the difference from the upstream repository this contract came from:
  **there, the justification was "the repository's own checks run them". This
  repository has no CI test job at all** — `claude-code-review.yml` is its only
  workflow. So the reason here is narrower and worth stating plainly: you have a
  writable checkout, a network path and no isolation, and running a test suite or
  a dependency install from a review job is a side effect nobody asked for. It is
  **not** because something else has already run them. If a change looks untested,
  say it is untested; do not assume a green suite exists somewhere.
- **Do not claim that tests, linters, or type checks pass or fail.** You have not
  run them and will not.
- **Do not relitigate settled decisions.** These are deliberate and documented in
  the source; treat them as given unless the pull request is explicitly
  redesigning them:
  - `settings.py` is a deliberately tiny dataclass holding two keys, read at
    import, mutated directly by `tests/conftest.py`. Everything else is read from
    `os.environ` at the point of use.
  - The transport is chosen by branching on the resolved value, never by probing
    `hasattr(mcp, "streamable_http_app")` — the probe always selected Streamable
    HTTP and made `--sse` serve `/mcp`.
  - An unrecognised transport warns and falls back to stdio rather than exiting.
  - The server does not hard-fail when no search provider is configured; it warns
    and comes up so tool discovery still works.
  - The failure policy in `content/resolver.py` is deliberately **not** uniform:
    Stack Exchange and arXiv return an error note with no HTML fallback, the
    other three fall back to the universal loader first. arXiv is PDF-based and
    the universal loader intentionally skips PDFs.
  - `mcp>=1.25,<2` is bounded because 2.0.0 removed `mcp.server.fastmcp`; the
    bound lifts only alongside a port to the 2.x API.
  - `starlette` and `uvicorn` are declared directly although they also arrive
    transitively — `server.py` imports both.
  - The `nodriver` encoding monkey-patch is a load-bearing workaround for a
    non-UTF-8 file in an installed third-party package, not accidental
    complexity.
  - `requirements.txt` is a `pip freeze` kept for tooling; `pyproject.toml` is
    the source of truth.
  - Everything under `.github/review/scripts/` except `select_rules.py` is
    carried verbatim from the upstream repository so fixes stay portable.
- **Do not comment on formatting or style** a linter or formatter would catch.
- **Do not propose an alternative architecture.** If the design itself looks
  wrong, say so in one sentence and stop.
