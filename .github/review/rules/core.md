# Rule: shared core (`models.py`, `settings.py`, `utils/`)

These four surfaces are imported by every other module in the package. A change
here is a change to the search providers, the content resolvers, the scrapers and
the server at once — which is why this rule fans out to all four of their rule
files rather than standing alone.

## `models.py` — the wire format

`WebSearchResult`, `WebSearchResponse` and `GetContentResponse` are pydantic
models returned **directly** from the MCP tools. Their field names and their
`Field(description=...)` strings are not internal detail: they are what an MCP
client's model reads when it decides how to use a result.

- **A field rename or removal is a breaking change to every client.** There is no
  version negotiation on these shapes. Flag one that ships without the README's
  tool documentation moving with it.
- `page_content` is documented as **"Always a string"**. Code that can return
  `None` into it, or that leaves it unset, contradicts a promise a client's model
  has been told it can rely on. Trace any new path that populates it.
- `diagnostics` is `None` unless `KINDLY_DIAGNOSTICS` is enabled. A change that
  makes it populated by default puts internal stage names and timings into every
  tool response.
- A `Field(description=...)` edit is a **prompt change**, because that text
  reaches the calling model. Judge it as you would judge a prompt: is it accurate
  about what the field now contains?

## `settings.py` — deliberately tiny

`Settings` is a plain dataclass holding two API keys, instantiated once at import
as the module-level `settings`. Its own docstring says: *"keep this module
lightweight; it is imported by tests."*

- **That is a settled decision — do not propose moving it to pydantic-settings,
  adding validation, or making it lazy** unless the pull request is explicitly
  doing that work. `tests/conftest.py` mutates `settings.settings.serper_api_key`
  directly in a session-scoped autouse fixture, so a change to the object's
  construction or mutability breaks the whole suite in a way a reviewer should
  name up front.
- Note the asymmetry and check whether a change widens it: `settings.py` holds
  only `SERPER_API_KEY` and `SERPBASE_API_KEY`, while the other three providers
  and every `KINDLY_*` knob are read straight from `os.environ` at the point of
  use. New configuration that lands in `settings.py` for one provider and in
  `os.environ` for the next is drift; say which convention the change is
  following and whether it matches its neighbours.
- **Import-time reads are load-bearing.** `Settings`' defaults are evaluated when
  the class body executes, so an environment variable set after import is
  ignored. A change that relies on late-set configuration will appear to work in
  a test that sets the variable early and fail for a user whose client sets it
  differently.

## `utils/diagnostics.py` — the redaction boundary

**This is the highest-severity file in this rule.** It is what keeps a user's
search API key, and a proxy password, out of stderr and out of tool responses.
`tests/test_diagnostics_masking.py` and
`tests/test_worker_launch_args_redaction.py` exist because getting it wrong
publishes a customer's credential.

Judge any change to it against these, and treat a weakening as **critical**:

- `_MASK_HINTS` (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `BEARER`) masks by
  **variable name**. A new credential-bearing variable whose name contains none
  of those hints is unmasked, and nothing goes red. If a change introduces one,
  that is a finding — name the variable.
- `_URL_USERINFO_RE` catches the case the name hints cannot: credentials inside a
  URL, as proxy variables carry them. Its comment records a deliberate trade-off
  — the character class admits `@` so the match runs to the **last** `@` before
  the path, covering passwords with an unescaped `@`, at the cost of possibly
  over-redacting a pathological value. **That over-redaction is the intended
  direction (it fails closed); do not "fix" it.** A change that makes the regex
  stricter — stopping at the first `@` — re-opens the exact hole the comment
  describes.
- `MAX_SAMPLE_CHARS`, `MAX_STDERR_CHARS` and `MAX_LINE_CHARS` bound what reaches
  a diagnostics payload. Raising one enlarges the blast radius of any redaction
  gap; a raise needs a stated reason.
- Redaction must happen **before** text is written, not after it is assembled for
  display. A new emit path that formats a value into a message and redacts the
  message afterwards is only as good as the regex; one that redacts the value at
  the source is not.

## `utils/logging.py`

Small, but it decides where log output goes. Under the **stdio** transport,
stdout is the JSON-RPC channel: **anything printed to stdout corrupts the
protocol stream and breaks the client's session.** A change that logs, prints or
warns to stdout rather than stderr is a critical finding even though nothing in
the test suite will catch it.

## Entry points

`__init__.py` defines the public surface and `__main__.py` is what `python -m
kindly_web_search_mcp_server` runs. Both are two-line files; a change to either
is almost always more significant than its size suggests, because they are what
the four `[project.scripts]` console entry points and the documented `uvx`
invocation resolve through. Check the change against `pyproject.toml`'s
`[project.scripts]` block — all four names must still resolve.
