# Add Tavily as a Backup Web Search Provider — Requirements

## As Is
- The MCP server exposes `web_search(query, num_results=3, return_full_pages=true)`.
- `web_search` currently queries **Serper** only, via `src/kindly_web_search_mcp_server/search/serper.py`.
  - Auth: `SERPER_API_KEY` in environment.
  - Endpoint: `POST https://google.serper.dev/search` with JSON `{ "q": "<query>", "num": <num_results> }`.
- If `SERPER_API_KEY` is not set, `web_search` fails.

## To Be
- `web_search` supports two providers:
  1) **Serper** (primary if configured)
  2) **Tavily** (secondary / fallback)
- The user provides either:
  - `SERPER_API_KEY`, or
  - `TAVILY_API_KEY`, or
  - both.
- Selection rules:
  - If both keys are present, **default to Serper**.
  - If the primary provider fails and the other provider is available, automatically **fallback** to the secondary provider.
  - If only one provider key is present, use that provider only (no fallback).
  - If no provider keys are present, `web_search` fails with a clear, actionable error.

## Requirements
1. Tavily search provider
   - Implement a Tavily-backed search function with behavior equivalent to Serper for our tool output:
     - input: `query`, `num_results`
     - output: list of `WebSearchResult(title, link, snippet, page_content=None)`
   - Use Tavily HTTP API (Bearer auth):
     - `POST https://api.tavily.com/search`
     - `Authorization: Bearer <TAVILY_API_KEY>`
     - JSON body includes:
       - `query` (required)
       - `max_results` (mapped from `num_results`)
       - `search_depth` default `basic`
       - disable extra payloads: `include_answer=false`, `include_images=false`, `include_raw_content=false`
   - Response mapping:
     - Tavily `results[].title` → `WebSearchResult.title`
     - Tavily `results[].url` → `WebSearchResult.link`
     - Tavily `results[].content` → `WebSearchResult.snippet`
2. Provider routing (selection + fallback)
   - Determine available providers from environment:
     - `SERPER_API_KEY` and `TAVILY_API_KEY`.
   - Routing rules:
     - If `SERPER_API_KEY` is set: primary = Serper; secondary = Tavily if `TAVILY_API_KEY` is set.
     - Else if `TAVILY_API_KEY` is set: primary = Tavily; secondary = none.
   - Failure handling:
     - Fallback triggers only on transient/provider failures:
       - HTTP 5xx, HTTP 429
       - network/timeout errors
       - malformed/unparseable responses
     - Fallback does NOT trigger on:
       - missing/invalid API key (auth/config issues; e.g., HTTP 401/403)
       - HTTP 400 (client error)
       - empty result sets (0 results is a valid response)
     - If primary search raises a fallback-triggering error and a secondary provider exists, call secondary.
     - If both fail, return the primary error (with a short note that fallback also failed), without leaking secrets.
3. No API keys in outputs/logs
   - Never include API keys in returned Markdown or raised error messages.
4. Docs + env example
   - Update `README.md` and `.env.example` to mention `TAVILY_API_KEY` and precedence/fallback behavior.

## Acceptance Criteria (mapped to requirements)
1. With only `TAVILY_API_KEY` set, `web_search` returns results without requiring `SERPER_API_KEY`.
2. With both keys set, `web_search` uses Serper by default.
3. With both keys set and Serper returns an error (e.g., HTTP 500), `web_search` returns Tavily results instead.
4. With neither key set, `web_search` fails with an actionable error mentioning both env vars.
5. Errors never include the raw values of `SERPER_API_KEY` or `TAVILY_API_KEY`.
6. If the primary provider returns fewer than `num_results`, return the available results without error.

## Testing Plan (TDD)
- Unit tests (no network)
  - Tavily parser:
    - Use `httpx.MockTransport` to validate request formation (`Authorization: Bearer ...`) and response parsing.
  - Provider selection:
    - Only Tavily key → Tavily search invoked.
    - Both keys → Serper invoked, Tavily not invoked.
  - Fallback behavior:
    - Serper raises error → Tavily invoked and results returned.
    - Serper raises error and Tavily raises error → error propagated (no secrets).
    - Serper raises HTTP 401/403 → fallback NOT triggered.
    - Serper returns empty results → fallback NOT triggered.
    - Serper raises HTTP 429 → fallback triggered.
- Live integration tests (opt-in)
  - Similar to Serper live test pattern, gated behind `RUN_LIVE_TESTS=1` and presence of the relevant key(s).

## Implementation Plan (smallest safe increments)
1. Add `src/kindly_web_search_mcp_server/search/tavily.py` implementing Tavily search.
   - Test: unit test for request + response parsing via `MockTransport`.
2. Add a provider router `search_web(...)` (or similar) that selects Serper/Tavily and implements fallback.
   - Test: unit tests using mocks to assert call order and fallback behavior.
3. Switch `server.web_search` to call the router instead of calling Serper directly.
   - Test: update `tests/test_server.py` to patch the router rather than `search_serper`.
4. Update `README.md` and `.env.example` to include `TAVILY_API_KEY` and document precedence/fallback.
