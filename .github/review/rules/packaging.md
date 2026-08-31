# Rule: packaging and configuration

`pyproject.toml`, `requirements.txt`, `Dockerfile`, `.dockerignore`,
`.env.example`. This rule fans out to `mcp-server`, because the dependency bounds
here are what keep the server importable at all.

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

**Every new direct import needs a declared dependency.** That is the rule the
guard enforces; check a new import against `[project.dependencies]`.

## `requirements.txt` is not the source of truth

Its header says so: it is a `pip freeze` of a working environment, kept for
tooling that expects the file, and **`pyproject.toml` is the source of truth**.

- A dependency added to one and not the other is drift. Flag it, and say which
  file the reviewer should treat as authoritative — the answer is
  `pyproject.toml`.
- A change that starts *installing from* `requirements.txt` in a path users take
  reverses that decision and needs saying so.

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
