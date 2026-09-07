# Rule: search providers (`search/`)

Six backends — Serper, SerpBase, Tavily, SearXNG, Sofya and You.com — behind one
registry. They share a single contract, which is why they share one rule file.

## `PROVIDERS` is the single source of truth

`search/__init__.py`'s own docstring states it: `PROVIDERS` is where providers
exist, what configures them, and **the order they are selected in**. The router,
the startup preflight in `server.py`, and the `search.provider_select`
diagnostics payload all read from it, and tests assert the documentation matches.

- **Adding a provider is one `SearchProviderSpec` entry plus its module.** A
  change that adds a provider by special-casing it anywhere else — an `if` in the
  router, a second env-var lookup in `server.py` — is a finding even if it works.
- `tests/test_provider_registry_consistency.py` is the guard. A `PROVIDERS` edit
  with no movement there means the registry and its documentation can drift
  silently.
- **Order is behaviour, not style.** `PROVIDERS` order decides which configured
  provider serves a request when a user has set more than one key. Reordering it
  changes which service a user is billed for. Flag a reorder that the pull
  request description does not mention.
- `SearchProviderSpec` resolves its coroutine **by attribute name at call time**,
  not by holding a reference, and the re-exports in `__all__` exist so tests can
  substitute providers by rebinding module attributes. The module comment says
  so. Removing an entry from `__all__` as "unused" — which is exactly what a
  linter will suggest — breaks the substitution seam. Do not propose it.

## The per-provider contract

Every provider module must:

1. Read **its own** environment variable, the one named in its
   `SearchProviderSpec.env_var`, and nothing else.
2. Return `list[WebSearchResult]` with all four required fields populated —
   `page_content` is documented as always a string, so a provider that leaves it
   empty must leave it `""`, never `None`.
3. Let an `httpx.HTTPError` reach the router, which converts it. **This rule
   used to say the opposite** — that an `httpx` exception escaping to the router
   was a finding — and it described something that never existed: no provider
   raises `WebSearchProviderError`, and five of the six call `raise_for_status()`
   and let httpx's exception out. The router is now the enforcement point.
   `search_web` catches the whole `httpx.HTTPError` family and re-raises
   `SearchProviderTransportError`, whose message is built from the provider's
   label and the HTTP status, chained `from` the original so the status stays
   readable through `__cause__`.

   The finding is therefore the opposite shape: **a provider that catches an
   `httpx` error and re-quotes its message is a finding**, because that message
   contains the request URL and the conversion at the router can no longer remove
   it. `searxng.py`'s aggregate is the one place this shape exists; it is
   measured not to disclose today and is called out in TEST_SUITE.md §14. A
   provider that wraps `raise_for_status()` to "comply" with the old wording
   bypasses the conversion entirely.
4. **Never put the API key anywhere but the request it authenticates.** Not in a
   log line, not in an exception message, not in a diagnostics payload. Check
   every new error path — a common shape is `raise ... f"{response.text}"` where
   the provider echoed the request back.

   Two credentials do not travel in a header and so are reachable through a URL:
   SerpBase's `api_key` query parameter and SearXNG's base-URL userinfo. Both
   have disclosed to the MCP client in the past — see TEST_SUITE.md §14 — so
   **anything derived from a request URL or from `SEARXNG_BASE_URL` is
   credential-bearing until shown otherwise**, including a log line. Redaction is
   not automatically the answer: `redact_url_credentials` matches `://user:pass@`
   and is measured useless on a value with no scheme, which is exactly what
   reaches the "no valid URLs" branch. Prefer dropping the value over filtering
   it. `tests/test_provider_credential_disclosure.py` holds this.

`serpbase.py` is **not** a base class, despite the name — nothing imports from
it but the registry, and each provider module stands alone. This paragraph used
to say a change there applied to every provider built on it; it does not, and a
reviewer acting on that would look for consequences that cannot exist.

## Response normalisation

Each provider maps a different response shape onto `WebSearchResult`. Judge the
mapping against the source API's actual shape, and specifically:

- **Missing fields.** A provider that indexes into a response dict without a
  default crashes the whole tool call on one malformed result. Prefer dropping a
  result to failing the search — but say so, because silently dropping results is
  the other failure mode and it is invisible.
- **Empty results are a valid answer**, not an error. A provider that raises on
  zero hits turns "nothing found" into "search is broken".
- `num_results` is a request, not a guarantee. Check the provider actually bounds
  what it returns rather than trusting the upstream service to honour the
  parameter.

## SearXNG is different, and the difference matters

`SEARXNG_BASE_URL` points at a **user-supplied, self-hosted** instance. Its
response is not a trusted API contract the way Serper's is:

- Treat the URL as untrusted input for SSRF purposes — it is user-controlled and
  the server will fetch it.
- Treat the response body as untrusted content. Every `link` it returns is fed
  onward to the content resolver and then to the scraper.
- A change that makes the SearXNG path share a code path with the commercial
  providers should be checked for whether it also shares their trust assumptions.

## Timeouts

Every outbound call needs a bounded timeout. `httpx`'s default is not one you
should rely on being present — a provider that constructs a client without an
explicit timeout can hang the whole tool call up to the server's own
`KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS` budget, which is the ceiling, not the
intent. Flag an unbounded call.

## Tests

Each provider has a `tests/test_<provider>_unit.py`. `tests/test_serper_live.py`
is the opt-in live test and is gated on a real key being present — a unit test
that reaches the network is a finding, and so is a live test that runs
unconditionally.
