# Tool Description Improvements — `web_search` + `get_content`

This task focuses on improving the *tool descriptions* (the `@mcp.tool()` docstrings) so that
coding agents (Claude Code, Codex CLI, Cursor, etc.) reliably:
- Select the correct tool (“tool discovery”).
- Provide correct inputs (query vs. URL).
- Interpret outputs correctly without extra back-and-forth.
- Avoid context/token “bombs” from overly broad calls.

## As Is
- `src/kindly_web_search_mcp_server/server.py` defines two MCP tools:
  - `web_search(query: str, num_results: int = 3) -> dict`
  - `get_content(url: str) -> dict`
- Both tools already work and have docstrings, but the descriptions are mostly “implementation focused”
  (pipeline details) and do not explicitly encode *agent-facing guidance* that improves:
  - tool choice (when to use which tool),
  - parameter selection (query vs. URL, choosing small `num_results`),
  - expectations (best-effort extraction, truncation, PDFs/paywalls, required API keys),
  - and safe/debugging-oriented usage patterns for coding agents.

## To Be
- `web_search` description is optimized for coding agents:
  - Front-loads what the tool does in one sentence.
  - Explicitly states when to use it (debugging errors, checking API signatures/interfaces, confirming package versions, etc.).
  - Explicitly states when *not* to use it (when a specific URL is already known; use `get_content`).
  - Documents required configuration (`SERPER_API_KEY` or `TAVILY_API_KEY`) and common failure modes.
  - Warns about context size and steers agents to keep `num_results` small.
  - Documents the output schema in a way that helps agents parse it programmatically.
- `get_content` description is optimized for coding agents:
  - Front-loads what the tool does in one sentence.
  - Explicitly states when to use it (user-provided URL; URL discovered via search; deep dive on a single page).
  - Documents best-effort limitations (anti-bot/paywalls; some PDFs unsupported) and any deterministic fallbacks.
  - Documents the output schema clearly.

## Requirements
1. `web_search` tool description
   - Must contain:
     - “What it does” (web search + content fetch + Markdown extraction).
     - “When to use” with coding-agent-relevant examples (debugging errors; double-checking APIs; version checks).
     - “When not to use” guidance (prefer `get_content` if you already have a URL).
     - Key constraints and prerequisites:
       - Requires `SERPER_API_KEY` or `TAVILY_API_KEY`.
        - Best-effort extraction; can be truncated; some URLs may not yield content.
        - Explicit guidance on keeping `num_results` small to avoid large outputs (include a recommended range).
        - Error/failure behavior guidance:
          - What happens if no search provider API key is configured.
          - What `page_content` looks like when extraction fails (deterministic Markdown note, never `null`).
     - Output schema description that matches actual return shape.
2. `get_content` tool description
   - Must contain:
     - “What it does” (fetch single URL + Markdown extraction).
     - “When to use” (URL known; user-provided URL; follow-up after search).
     - “When not to use” guidance (prefer `web_search` when you need to discover URLs first).
     - Key constraints:
        - Best-effort extraction; can be truncated; some PDFs/unsupported types may not yield content.
        - Error/failure behavior guidance:
          - What `page_content` looks like when extraction fails (deterministic Markdown note, never `null`).
     - Output schema description that matches actual return shape.
3. The updated descriptions must remain truthful and consistent with the actual behavior of the tools.

## Acceptance Criteria
1. `web_search.__doc__` includes a “When to use” section with at least 2 coding-agent scenarios (e.g., debugging errors, verifying API signatures, checking package versions).
2. `web_search.__doc__` includes a “When not to use” section that explicitly references `get_content`.
3. `web_search.__doc__` mentions both `SERPER_API_KEY` and `TAVILY_API_KEY` in a configuration/prerequisites context (not only as standalone tokens).
4. `web_search.__doc__` documents `num_results` with its default and a recommended range (context control).
5. `web_search.__doc__` documents the return shape including `results[*].page_content`, and states that `page_content` is always a string (with a deterministic note on failure).
6. `get_content.__doc__` includes a “When to use” section and a “When not to use” section that references `web_search`.
7. `get_content.__doc__` documents the return shape `{url, page_content}` and states that `page_content` is always a string (with a deterministic note on failure).
8. Existing unit tests continue to pass and a new unit test enforces the above with semantic/pattern-based checks (not exact wording matches).

## Testing Plan (TDD)
- Unit tests (no network):
  - Validate `web_search.__doc__` using semantic/pattern checks:
    - env var names appear near “required/config/prerequisite”
    - `num_results` appears near “recommended/range/context/token/limit”
    - `get_content` appears near “when not to use/avoid/prefer”
  - Validate `get_content.__doc__` using semantic/pattern checks:
    - `web_search` appears near “when not to use/avoid/prefer”
    - “PDF/unsupported” limitations are mentioned
    - return shape `{url, page_content}` is described
- Regression tests:
  - Run the existing test suite to ensure no behavior changes were introduced.

## Implementation Plan (smallest possible changes)
1. Update the `web_search` docstring in `src/kindly_web_search_mcp_server/server.py`.
   - Test: new docstring unit test passes; existing tests still pass.
2. Update the `get_content` docstring in `src/kindly_web_search_mcp_server/server.py`.
   - Test: new docstring unit test passes; existing tests still pass.
3. Add `tests/test_tool_descriptions.py` validating the acceptance criteria keywords.
   - Test: `python -m pytest -q` passes.
