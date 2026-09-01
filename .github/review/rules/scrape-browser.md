# Rule: universal retrieval and the browser (`scrape/`)

The fallback path for every URL no site-specific handler claims: HTTP fetch, HTML
extraction and sanitising, and a headless-Chromium loader driven through
`nodriver`. `nodriver_worker.py` (~1,280 lines) and `universal_html.py` (~1,320)
are the two largest files in the repository.

**This is the only part of this server that starts a subprocess and hands it a
command line.** That is a different risk class from parsing a JSON API, and it is
why this rule is separate from `content-resolvers`.

## Launch arguments are a credential surface

`_resolve_chrome_proxy` reads `KINDLY_CHROME_PROXY`, and a proxy URL routinely
carries `user:pass@`. Those values become **process command-line arguments**,
which are visible to any other process on the machine and land in any log that
echoes the launch.

- `nodriver_worker.py` imports `redact_url_credentials` from
  `utils/diagnostics` *"so the redaction keeps one definition of what a
  credential looks like"*. A change that formats a launch argument into a log,
  a diagnostics payload or an error message **without** passing it through that
  helper is a **critical** finding.
- `tests/test_worker_launch_args_redaction.py` is the guard. A change to how
  arguments are built or reported, with no movement there, is a finding.
- A second definition of "what a credential looks like" anywhere in this package
  is itself the defect — the single definition is the point.

## The sandbox

`_resolve_sandbox_enabled` reads `KINDLY_NODRIVER_SANDBOX`, and `.env.example`
ships it as `0`. `tests/test_nodriver_worker_launch_resolvers.py` covers it.

- **Running Chromium with `--no-sandbox` on untrusted pages is a real exposure**,
  and this server's whole job is loading pages chosen by a search engine. The
  default is what it is for container and CI compatibility; that is a documented
  trade-off, not something to silently widen.
- A change that removes the ability to turn the sandbox **on**, or that ignores
  the variable on some path, is a critical finding.
- A change that adds a new Chromium flag needs a stated reason. Flags that
  disable isolation, web security, certificate checks or CORS are the ones to
  challenge; `_base_browser_args` is where they live.

## Process and port lifecycle

`ChromiumPool` / `ChromiumSlot` keep browsers warm
(`KINDLY_NODRIVER_REUSE_BROWSER=1`, `KINDLY_NODRIVER_BROWSER_POOL_SIZE=1`).

- **Every acquire must have a matching release on every path, including the
  exception path.** A leaked slot with a pool size of 1 wedges the server for
  every subsequent request until `KINDLY_NODRIVER_ACQUIRE_TIMEOUT_SECONDS`
  expires — and then for the next one too.
- Both `terminate`/`terminate_sync` and `shutdown`/`shutdown_sync` exist because
  teardown happens from both async and interpreter-exit contexts. A new teardown
  path that only handles one leaves orphaned Chromium processes on the user's
  machine. Check `_register_shutdown` still covers the change.
- `_pick_port` / `_pick_free_port` / `KINDLY_NODRIVER_PORT_RANGE`: a change that
  reintroduces a check-then-bind race between choosing a port and starting the
  browser produces a failure that only appears under concurrency.
- **A reused browser carries state.** Cookies, storage and service workers
  survive between requests to different sites when reuse is on. A change that
  starts storing more per-page state should say what stops site A's state
  reaching site B.

## Retries must not amplify

`KINDLY_NODRIVER_RETRY_ATTEMPTS`, `KINDLY_NODRIVER_RETRY_BACKOFF_SECONDS` and
`KINDLY_NODRIVER_SNAP_BACKOFF_MULTIPLIER` bound startup retries, and
`_is_retryable_browser_connect_error` decides what is worth retrying.

- Widening that predicate to retry a non-transient failure turns one failed
  request into N browser launches. Check a new error class is genuinely
  transient.
- Every retry loop must respect the caller's budget. The tool-level ceiling is
  `KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS`; a retry loop that can outlive it produces
  a hang the caller cannot distinguish from a slow page.
- `KINDLY_HTML_TOTAL_TIMEOUT_SECONDS` bounds the load itself. A new await with no
  timeout inside that path is a finding.

## Environment mutation is global

`_ensure_no_proxy_localhost` and `_split_no_proxy_value` **modify the process
environment** so that loopback traffic bypasses a configured proxy.

- Mutating `os.environ` affects every other library in the process, including
  `httpx` in the search providers. A change that adds another such mutation needs
  to say what else it affects; a change that removes this one re-introduces the
  bug where the pool's own DevTools connection is sent through the user's proxy.
- Same for `KINDLY_CHROME_PROXY_BYPASS`: it and `NO_PROXY` must not disagree, or
  the browser and the HTTP client take different routes for the same host.

## Monkey-patching `nodriver`

`_patch_nodriver_network_encoding`, `_clear_nodriver_modules`,
`_is_nodriver_network_path`, `_inject_encoding_cookie` and
`_is_non_utf8_syntax_error` exist to work around a non-UTF-8 source file inside
the installed `nodriver` package.

**This is a deliberate, load-bearing workaround, not accidental complexity.** It
is fragile by nature: it is pinned to the shape of a third-party file. Judge a
change to it on whether it still fails *safely* when the upstream file changes —
the patch not applying should degrade to the original error, never to a silent
wrong result. Do not propose deleting it without evidence the upstream defect is
gone.

## Extraction and sanitising

`extract.py`, `sanitize.py` and `universal_html.py` turn arbitrary HTML into
Markdown.

- **The output goes straight into a model's context.** Sanitising is not only an
  XSS concern here; it is what keeps script content, hidden text and markup
  artefacts out of a prompt.
- `KINDLY_MARKDOWN_SUFFIX_HOSTS` and `KINDLY_MARKDOWN_ACCEPT_PROBE` are host-specific
  behaviour driven by configuration. A new host-specific branch hard-coded in
  source rather than driven by one of these is drift — say so.
- The universal loader **intentionally skips PDFs**; `content/arxiv.py` depends on
  that fact (it does not fall back here because of it). A change that adds PDF
  handling to this path must say what it does to arXiv's no-fallback decision.
- Bound the output. An unbounded extraction from a hostile page is a
  context-window exhaustion attack on the client.

## Tests

`test_universal_html_loader.py`, `test_nodriver_worker_sandbox.py`,
`test_nodriver_worker_launch_resolvers.py`, `test_worker_launch_args_redaction.py`,
`test_content_resolver_universal_fallback.py`.
These files are large and hard to test end to end — which raises rather than
lowers the bar for testing the *decisions*: which flags are built, what gets
redacted, what is retried, what is bounded. A change to any of those five with no
test is a finding.

The split between the first two is deliberate and worth keeping. Flag and
default resolution — sandbox, browser executable, retry attempts, the Chromium
command line — belongs in `test_nodriver_worker_launch_resolvers.py`, against
the resolvers directly. `test_nodriver_worker_sandbox.py` keeps only what needs
`_fetch_html` itself: retry, termination and profile cleanup. A new flag
assertion added to the second file, or a browser started to check a boolean, is
a finding — routing flag assertions through `_fetch_html` is what let a
signature change silently disable eight tests at once.
