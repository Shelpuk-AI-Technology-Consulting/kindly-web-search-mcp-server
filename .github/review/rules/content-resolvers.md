# Rule: content resolvers (`content/`)

Five site-specific handlers — Stack Exchange, GitHub Issues, GitHub Discussions,
Wikipedia, arXiv — plus `resolver.py`, the dispatcher that chooses between them
and falls through to the universal HTML path.

## The dispatch contract

`resolve_page_content_markdown` runs the handlers **in a fixed order**, and each
stage has the same shape: call the handler's `parse_*_url`, treat its exception
as "not my URL", and otherwise commit to that handler.

- **The `try/except/else` shape is the contract, and it is easy to break.** The
  `else` branch runs only when `parse_*_url` did *not* raise. A change that moves
  the fetch into the `try` makes a fetch failure look like a URL mismatch and
  silently falls through to the next handler — which will not match either, so
  the request lands on the universal scraper for a URL that had a perfectly good
  API handler.
- **Order is behaviour.** A new handler inserted above an existing one takes
  URLs the old one used to serve. Say so, and check the `parse_*_url` patterns
  are actually disjoint — two handlers that both accept a URL means the first
  wins forever and the second is dead code.
- A new handler means: the module, its `parse_*_url` and `fetch_*_markdown`, its
  error type, an entry in the dispatch chain, its `.env.example` knobs, and a
  test. Flag a partial addition.

## Failure policy is deliberately not uniform — check the change respects it

Read this table before flagging an inconsistency, because the inconsistency is
intentional and is recorded in the code:

| Handler | On failure |
|---|---|
| Stack Exchange | returns a short Markdown error note; **no** HTML fallback |
| GitHub Issues | falls back to `load_url_as_markdown`, then an error note |
| GitHub Discussions | falls back to `load_url_as_markdown`, then an error note |
| Wikipedia | falls back to `load_url_as_markdown`, then an error note |
| arXiv | returns an error note; **no** fallback |

The two that do not fall back have reasons in the source: **arXiv is PDF-based
and the universal HTML loader intentionally skips PDFs**, so a fallback would
reliably produce nothing. A change that makes the policy uniform "for
consistency" is a finding unless it says what it does about those two cases.

Conversely: the GitHub handlers fall back specifically so a missing
`GITHUB_TOKEN` or a rate-limit degrades to HTML rather than to nothing. A change
that removes that fallback removes the resilience it was added for.

## Error notes are a returned value, not an exception

Every failure path returns a Markdown string — `_Failed to retrieve ...
{type(e).__name__}_\n\nSource: {url}\n` — because `page_content` is documented as
always a string and the caller is a model, not a program.

- **Only the exception's *type name* goes into the note, never its message.**
  That is deliberate: an exception message can carry a token, a signed URL, or a
  chunk of the upstream response. A change to `{e}` or `{e!r}` is a **critical**
  finding.
- `resolver.py` says so in a comment — *"Best-effort: return a short Markdown
  error note (no secrets)"*. Hold new code to it.
- A bare `except Exception` here is deliberate, not a defect: the contract is
  that no handler failure escapes to the tool caller. Do not flag it as one, but
  do check the handler is not swallowing a programming error it should surface in
  diagnostics.

## Untrusted input, from both ends

Everything these handlers touch is attacker-influenceable: the URL comes from a
search result, and the body comes from a public site where anyone can post.

- **Prompt injection is the live risk.** The Markdown returned goes straight into
  a model's context. Content that instructs the reading model is not something
  this server can filter, but a change that *adds* rendering fidelity — inlining
  raw HTML, following embedded links, expanding includes — widens that surface
  and should say why it is worth it.
- Validate the parsed URL components before putting them into an API path.
  `parse_*_url` returning an owner/repo/number is the trust boundary; a handler
  that string-formats an unvalidated fragment into a request URL can be steered
  to a different endpoint.
- Every `*_MAX_CHARS` / `*_MAX_COMMENTS` bound in `.env.example`
  (`STACKEXCHANGE_MAX_CHARS`, `GITHUB_MAX_CHARS`, `GITHUB_MAX_COMMENTS`,
  `WIKIPEDIA_MAX_CHARS`, `ARXIV_MAX_CHARS`, `ARXIV_MAX_PAGES`) exists to bound
  what a hostile page can push into a client's context window. A new handler
  without one, or a change that raises one without saying why, is a finding.

## Credentials and identification

- `GITHUB_TOKEN` and `STACKEXCHANGE_KEY` are optional; the handlers must work
  without them and must not log them. Check any new error path.
- `WIKIPEDIA_USER_AGENT` and `ARXIV_USER_AGENT` are **required by those services'
  policies**, and `.env.example` ships them with a contact placeholder. A change
  that drops or hard-codes the User-Agent risks the server being blocked for
  every user, not just the one who made the change.

## Tests

Each handler has unit tests over parsing and over Markdown rendering
(`test_stackexchange_parsing.py`, `test_stackexchange_markdown.py`,
`test_github_issues.py`, and so on). A new `parse_*_url` needs both the URLs it
accepts and the URLs it must reject — a parser that is too permissive steals
traffic from the handler below it, and only a rejection test catches that.
