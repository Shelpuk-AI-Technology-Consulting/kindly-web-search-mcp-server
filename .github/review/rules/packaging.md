# Rule: packaging and configuration

`pyproject.toml`, `Dockerfile`, `.dockerignore`, `.env.example`, and
`requirements.txt` — which no longer exists, and is matched so that re-adding it
is reviewed rather than unnoticed. This rule fans out to `mcp-server`, because the
dependency bounds here are what keep the server importable at all.

## The install path makes bounds load-bearing

The documented install is `uvx --from git+https://github.com/...`, which
**re-resolves dependencies from PyPI on every start** and ignores any lockfile.
There is no pinned deployment between a bound change and a user's next launch.

That is why `pyproject.toml` carries the comments it does, and both are settled:

- **`mcp>=1.25,<2`.** `server.py` imports `FastMCP` from `mcp.server.fastmcp`,
  which the SDK **removed in 2.0.0**. Leaving this unbounded broke every user the
  moment 2.0.0 shipped. The comment says the bound may be lifted **only alongside
  a port to the 2.x `MCPServer` API** — treat a lift without that port as a
  **critical** finding.
- The `>=1.25` floor is "oldest release verified against this server", stopping a
  constrained resolve from silently selecting an older, untested API. A change
  that lowers it needs a statement of what was verified.
- `starlette` and `uvicorn` are declared **directly** although they arrive
  transitively via `mcp`, because `server.py` imports both to wrap the ASGI app
  in CORS middleware and serve it. Removing them as "redundant" is exactly the
  undeclared-dependency failure `tests/test_dependency_constraints.py` exists to
  catch.
- `packaging` sits in the `dev` extra for the same reason: the dependency-bound
  guard imports it directly rather than inheriting it from pytest.
- **Every runtime dependency is bounded, and every bound is machine-checked**
  against the runtime table in TEST_SUITE.md §10.2, in both directions. Nine of
  the ten were bare names until that table existed. The guard also asserts that
  each ceiling **excludes the next major** computed from the version the bound was
  verified against — a check a table-versus-table comparison cannot make, because
  a ceiling widened in both places at once agrees with itself. Loosening one is a
  **critical** finding unless the PR records a fresh resolve.
- Bounds are chosen **against a real resolve, not by inspection**: `mcp` constrains
  several of these itself, and asks for `starlette` with no ceiling at all — which
  is how `starlette` crossed 0.x → 1.x here unnoticed.

**Every new direct import needs a declared dependency.** That is the rule the
guard enforces; check a new import against `[project.dependencies]`.

## `requirements.txt` was deleted, and re-adding it needs an argument

It used to exist as a `pip freeze` of a working environment, kept for tooling
that expects the file, while `pyproject.toml` was the source of truth. **No
install path ever read it** — the `Dockerfile` runs `pip install .` and the
README documents `uvx --from git+…` — so it drifted, and a stale freeze on a
public repository is not inert: at deletion it carried 31 open Dependabot alerts,
14 rated high, and all five open Dependabot pull requests, against a file nothing
installs from. Fake alarms at that volume train people to ignore the security tab,
which is worse than having no freeze file. Alerts reachable through
`requirements-ratchet.txt` or `pyproject.toml` are unaffected and remain real.

**It had also stopped listing a runtime dependency.** It carried no `trafilatura`
while `pyproject.toml` declared it and `src/` imported it, so
`pip install -r requirements.txt` produced a server that could not extract
content. That is what a freeze nobody installs from decays into, and it is why the
answer was deletion rather than a refresh — and note that `packaging.md`'s own
drift rule had been violated for some time with nothing to catch it.

This rule still lists `requirements.txt` in its patterns **deliberately**, for a
file that is not there: it means a pull request that re-introduces one is routed
to this rule rather than sliding in unreviewed.

- A PR that adds `requirements.txt` back must say what reads it. "For tooling" is
  the answer that already failed once; a second freeze nobody installs from
  recreates the alert noise on a schedule.
- A change that starts *installing from* such a file in a path users take
  reverses the decision that `pyproject.toml` is the source of truth, and needs
  saying so.

## The optional extras

`pdf-advanced` (`pymupdf-layout`, `pymupdf4llm`) is optional **on purpose**: it
pulls `onnxruntime` transitively, which may have no wheels for the newest CPython
releases, and a hard dependency would turn that into an install failure for
everyone. `requires-python = ">=3.13"` and the extras' own
`python_version < '3.14'` markers are part of that arrangement.

A change that promotes these to required dependencies breaks installation on the
Python versions the markers exclude. A change that touches the markers should say
which interpreter versions it was checked against.

## `.env.example` is the configuration contract

It is the only complete list of what this server reads — roughly forty variables
across search keys, per-resolver bounds, transport settings and the `KINDLY_*`
browser knobs.

- **A new environment variable that does not appear here is undocumented
  configuration.** Flag it. Include the comment: several entries here explain a
  trade-off (`KINDLY_NODRIVER_SANDBOX=0`, the proxy bypass list) and a bare
  `NAME=` teaches nothing.
- A **default changed in code** must be changed here too, and vice versa — this
  file is what users copy.
- **Never commit a real value.** `SERPER_API_KEY=` and friends ship empty;
  `WIKIPEDIA_USER_AGENT` / `ARXIV_USER_AGENT` ship with an obvious
  `you@example.com` placeholder. A change that fills one in with something that
  looks real is a critical finding whether or not the value works.

## `Dockerfile` and `.dockerignore`

- The container serves the HTTP transports, so it inherits everything in the
  `mcp-server` rule about host and origin allowlists. **A container that binds
  `0.0.0.0` with the loopback allowlist defaults is unreachable, and a change
  that "fixes" that by widening the allowlist rather than by setting
  `FASTMCP_ALLOWED_HOSTS` is a critical finding** — README § *"Host and origin
  allowlists"* documents the intended way.
- `.dockerignore` decides what reaches the build context. A change that lets
  `.env`, `.git` or a local virtualenv in is a secret-leak and image-bloat
  finding in one.
- Check the Chromium story: `get_content`'s fallback needs a browser, and the
  README documents installing one. An image change that removes it, or that
  changes where `KINDLY_BROWSER_EXECUTABLE_PATH` should point, must move the
  README with it.
- Pin what the image installs. An unpinned base tag or package makes the built
  image differ from the one that was reviewed.

## Version

`version` in `pyproject.toml` is the published version. A behaviour change that
users install through `uvx` should say whether it moves; a bound change that
breaks compatibility certainly should.
