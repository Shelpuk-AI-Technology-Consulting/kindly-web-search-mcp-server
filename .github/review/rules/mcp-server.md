# Rule: the MCP surface (`server.py`, `cli.py`)

`server.py` is the product's entire interface: the two tools, their descriptions,
the transport, and the network controls that decide who may reach it. `cli.py` is
the same component from the other end — what `uvx` runs.

## The tool descriptions ARE the product

`web_search` and `get_content` are declared with `@mcp.tool()`, and their
**docstrings are sent to the calling model as the tool specification.** They are
not developer documentation; they are the prompt that decides whether an agent
reaches for this server at all.

- A docstring edit is a **behaviour change**. Judge it as a prompt: is each claim
  still true of the code below it? Does the "When to use" / "When not to use"
  split still point `get_content` at the case where a URL is already known?
- `tests/test_tool_descriptions.py` exists to hold these stable. A description
  change with no movement there is a finding.
- Parameter defaults documented in the docstring must match the signature.
  `num_results` defaults to `3` and the text recommends `1–5`; a signature change
  that leaves the text saying otherwise misleads every client.
- The README documents these tools too. Code, docstring and README must agree —
  a change to any one of the three without the others is drift, and the README is
  what users read before installing.

## Transport resolution

`_resolve_transport` reads the CLI flag first, then `FASTMCP_TRANSPORT`,
normalises `http` to `streamable-http`, accepts `stdio` / `sse` /
`streamable-http`, and **warns and falls back to stdio** on anything else.

Settled decisions — do not relitigate them:

- **An unrecognised transport warns rather than exits.** Failing closed would
  make a typo in an MCP client's config look like a crashed server.
- **The ASGI app is chosen by branching on the resolved transport, not by probing
  `hasattr(mcp, "streamable_http_app")`.** The comment in `server.py` records why:
  `streamable_http_app` exists on every supported SDK version, so the probe always
  selected Streamable HTTP and `--sse` served `/mcp` while `/sse` returned 404.
  A change back to capability-probing re-introduces that bug.
- `--mount-path` is passed to `mcp.sse_app(...)`; the `TypeError` fallback around
  `mcp.run(...)` is there for older SDKs that do not accept `mount_path`. A bare
  `except TypeError` here is deliberate, not sloppy.

## Host and origin allowlists — the security surface

This is the highest-severity area in this rule. `_resolve_transport_security`
builds both the SDK's `TransportSecuritySettings` and the CORS origin list **from
one source**, and `_cors_origin_regex` compiles the second from the first.

- **DNS-rebinding protection stays on unconditionally.** An unset allowlist falls
  back to the loopback defaults (`LOCALHOST_ALLOWED_HOSTS` /
  `LOCALHOST_ALLOWED_ORIGINS`), never to "allow everything". A change that lets
  an empty `FASTMCP_ALLOWED_HOSTS` or `FASTMCP_ALLOWED_ORIGINS` disable the check
  is **critical**: combined with a permissive CORS policy it lets any web page the
  user visits drive their local server.
- **The two surfaces must be compiled from the same list.** Starlette's
  `CORSMiddleware` matches origins by exact string while the SDK's patterns
  support wildcards; that mismatch is why `_cors_origin_regex` exists. A change
  that sets `allow_origins` and `allowed_hosts` independently will approve a
  preflight the transport-security middleware then rejects with a `403` — and the
  README's troubleshooting section documents exactly that symptom.
- `allow_credentials=False` is deliberate; the comment says `True` would require
  explicit methods and headers. Flipping it while keeping `allow_methods=["*"]`
  and `allow_headers=["*"]` is a critical finding.
- `expose_headers` carries `mcp-session-id` and `mcp-protocol-version`. Dropping
  either breaks session resumption for browser clients.
- Any change to these must move README § *"Host and origin allowlists (`421` and
  `403` errors)"* in the same pull request.

## Startup behaviour

- **The server does not hard-fail when no provider is configured.** It logs the
  warning from `_provider_configuration_warning()` and comes up, because many
  clients set environment variables in their MCP config and still expect tool
  discovery to work. A change to fail fast here breaks that; it is a design
  reversal and needs saying so explicitly.
- The TTY guard on `--stdio` (`SystemExit(2)` unless `MCP_ALLOW_TTY_STDIO` is
  set) exists so a human running the command by hand gets an explanation rather
  than a hung process. Keep the override.
- **stdout belongs to the JSON-RPC stream under stdio.** Every diagnostic the
  startup path emits goes to stderr. A new `print()` without
  `file=sys.stderr` corrupts the protocol for every stdio client — critical, and
  invisible to the test suite.

## Timeouts and concurrency

`_resolve_tool_total_timeout_seconds`, `_resolve_web_search_max_concurrency` and
the `_get_int_env` / `_get_float_env` helpers all **clamp rather than reject**:
an unparseable value falls back to the default, and concurrency is bounded to
`1..5` and then to `num_results`.

- That is the convention. A new knob that raises `ValueError` on a bad value, or
  that accepts an unbounded one, is inconsistent — and an unbounded concurrency
  value means one tool call opening an unbounded number of browser tabs.
- The 120 s default with a 600 s ceiling is deliberate and its comment explains
  why the historical 55 s clamp was lifted (Windows headless cold starts). Do not
  propose restoring it.
- Every knob added here must appear in `.env.example` with a comment, and in the
  README if a user would ever need to set it.

## `cli.py`

Thin by design: it is the `kindly-web-search-mcp-server` console script and
`tests/test_uvx_cli.py` covers it. The documented install path is
`uvx --from git+https://...`, which **re-resolves dependencies from PyPI on every
start**. So anything `cli.py` assumes about the installed environment must hold
for a fresh resolve, not just for the developer's checkout.
