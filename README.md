# Kindly MCP Server: Web Search + Robust Content Retrieval

This repository contains a Python MCP server focused on **web search + high-quality content retrieval** for LLM agents.

Core idea:
- Use **Serper** for search results (title/link/snippet).
- For any URL, retrieve **LLM-ready Markdown** using a robust pipeline:
  - Prefer **source APIs** when available (StackExchange / GitHub Issues / Wikipedia / arXiv).
  - Fall back to a **universal HTML loader** (headless browser) for other sites.

## MCP Tools

### `web_search(query, num_results=3, return_full_pages=True)`
Searches the web and returns the top results.

When `return_full_pages=true`, each result is enriched with `page_content` (Markdown) using the same pipeline as `get_content`.

**Output shape**
```json
{
  "results": [
    {
      "title": "Example",
      "link": "https://example.com",
      "snippet": "Preview text…",
      "page_content": "# … (Markdown) …"
    }
  ]
}
```

### `get_content(url)`
Fetches a single URL and returns best-effort Markdown using the same content-resolution pipeline as `web_search(return_full_pages=true)`.

**Output shape**
```json
{
  "url": "https://example.com",
  "page_content": "# … (Markdown) …"
}
```

## Content Resolution Pipeline (How `page_content` Is Produced)

`content.resolver.resolve_page_content_markdown()` attempts these stages in order:
1. **StackExchange (StackOverflow + StackExchange network)** via StackExchange API.
2. **GitHub Issues** via GitHub GraphQL API.
3. **Wikipedia** via MediaWiki Action API (`parse`).
4. **arXiv** via arXiv Atom API + **PDF → Markdown** (PDF-first, in-memory).
5. **Universal HTML loader** fallback via headless `nodriver` (subprocess) → HTML → Markdown.

Important notes:
- The universal HTML loader intentionally **skips obvious PDFs**. Generic PDF support is not implemented yet.
- arXiv is handled as PDF (by design).
- All retrieval is **best-effort**; anti-bot measures, paywalls, or rate limits may prevent full extraction.

## Output Limits (Defaults)

To avoid “context bombs”, content is capped per source:
- StackExchange: `STACKEXCHANGE_MAX_CHARS` default `20000`
- GitHub Issues: `GITHUB_MAX_CHARS` default `20000`
- Wikipedia: `WIKIPEDIA_MAX_CHARS` default `50000`
- arXiv: `ARXIV_MAX_CHARS` default `50000`
- arXiv page cap: `ARXIV_MAX_PAGES` default `30`

When truncated, the Markdown includes a `…(truncated)` marker (and/or a truncation note).

## Configuration (Environment Variables)

This server expects environment variables to be provided by the runtime (IDE run configuration, shell export, or container env). The application code **does not auto-load** a local `.env` file.

### Required
- `SERPER_API_KEY`: Serper API key used for search requests.
  - Get it from Serper: create an account and generate an API key.

### Required (System Dependency)
- A **Chromium-based browser** is required for the universal HTML loader (`nodriver`) used by `get_content()` and by `web_search(return_full_pages=true)`.
  - `uv` / `pip` can install Python dependencies, but **cannot install system browsers** (Chrome/Chromium/Edge/Brave). Users must install a browser separately, or run via Docker.
  - If nodriver can’t auto-detect a browser, set `KINDLY_BROWSER_EXECUTABLE_PATH` (or `CHROME_BIN`) to the full path of the browser binary.
  - WSL note: install the **Linux** browser inside WSL (a Windows Chrome install is not visible as a Linux binary).

### Optional (Recommended)
- `GITHUB_TOKEN`: GitHub Personal Access Token used to retrieve GitHub Issue threads via GitHub APIs.
  - Create it in GitHub → Settings → Developer settings → Personal access tokens.
  - For public repos, a **read-only token** is highly recommended: it enables richer `get_content()` / `web_search(return_full_pages=true)` results for GitHub Issues pages. It is not mandatory, but without it GitHub Issues URLs may fall back to generic HTML extraction and produce a poorer representation (missing full thread context).
  - For classic tokens: `public_repo` is usually enough for public repositories. For private repos: `repo` (or equivalent fine-grained read permissions).
- `WIKIPEDIA_USER_AGENT`: Wikimedia asks API clients to use a descriptive User-Agent (ideally with contact info).
- `ARXIV_USER_AGENT`: User-Agent string for arXiv API + PDF requests.

### Optional (Tuning)
- `GITHUB_MAX_COMMENTS` (default `50`): max issue comments to fetch.
- `GITHUB_MAX_CHARS` (default `20000`): max Markdown characters returned for a GitHub issue thread.
- `STACKEXCHANGE_KEY`: optional StackExchange API key (higher quotas).
- `STACKEXCHANGE_FILTER`: StackExchange API filter (default `withbody`).
- `STACKEXCHANGE_MAX_CHARS` (default `20000`): max Markdown characters returned for a StackExchange thread.
- `WIKIPEDIA_MAX_CHARS` (default `50000`): max Markdown characters returned for a Wikipedia article.
- `ARXIV_MAX_CHARS` (default `50000`): max Markdown characters returned for an arXiv paper render.
- `ARXIV_MAX_PAGES` (default `30`): max PDF pages processed for arXiv.

## Installation

This project is a standard Python package. It exposes a console script and can also be run via `python -m ...`.

### Option A: `pip`
```bash
pip install .
```

For editable/dev installs:
```bash
pip install -e ".[dev]"
```

### Option B: `uv` (fast, zero-friction)
If you use `uv`, you can run without activating a virtualenv:
```bash
# When launched by an MCP client (stdio):
uv run -m kindly_web_search_mcp_server --stdio

# For manual testing (Streamable HTTP):
uv run -m kindly_web_search_mcp_server --http --host 127.0.0.1 --port 8000
```

### Environment variables
This server expects environment variables to be set by your runtime (IDE, shell, CI, container).

- Copy `.env.example` into your secret manager / IDE run config values (do not commit real secrets).
- Required: `SERPER_API_KEY`
- Recommended: `GITHUB_TOKEN`, `WIKIPEDIA_USER_AGENT`, `ARXIV_USER_AGENT`

## Running the Server

An entrypoint is provided via `pyproject.toml`:
- `mcp-server` → `kindly_web_search_mcp_server.server:main`
- `mcp-web-search` → `kindly_web_search_mcp_server.server:main` (recommended; less likely to conflict)
- `kindly-web-search` → `kindly_web_search_mcp_server.server:main` (alias)

Note: `--stdio` is meant to be launched by an MCP client. If you run it directly in a terminal and press Enter, the
server will try to parse your input as JSON-RPC and log errors. For interactive/manual runs, prefer `--http`.

Examples:
```bash
mcp-web-search --stdio
```

Streamable HTTP (useful for gateways / remote deployments):
```bash
mcp-web-search --http --host 0.0.0.0 --port 8000
```

Module form (handy in some environments):
```bash
python -m kindly_web_search_mcp_server --stdio
```

## Client Installation (Claude / Codex / Gemini / Cursor / Copilot)

This server supports both:
- **Local stdio** (most common): clients launch a local command and talk over stdin/stdout.
- **Streamable HTTP** (recommended for remote/container): clients connect to a URL.

Important: in **stdio** mode, stdout is reserved for the MCP protocol. This repo avoids printing on stdout during tool execution.

Note: the command you configure must be executable in the environment your client runs in. If you installed this server
into a virtualenv, prefer using an **absolute path** to the virtualenv executable (Claude Code does not run MCP servers
from your project directory, so relative paths often break).

### Claude Code

Prereq (WSL/Linux): install into a virtualenv so the executable exists.

If you used `uv`, it will typically create `.venv/` automatically; in that case the executable will be at:
- `$(pwd)/.venv/bin/mcp-web-search`

If you prefer a dedicated virtualenv for agents/automation in this repo, use `.venv-codex/`:
```bash
python -m venv .venv-codex
.venv-codex/bin/python -m pip install -U pip
.venv-codex/bin/python -m pip install -e .
```

CLI install (stdio):
```bash
claude mcp add --transport stdio kindly-web-search \
  --env SERPER_API_KEY="$SERPER_API_KEY" \
  --env GITHUB_TOKEN="$GITHUB_TOKEN" \
  -- "$(pwd)/.venv/bin/mcp-web-search" --stdio

# If you used `.venv-codex/` instead of `.venv/`, use:
#   -- "$(pwd)/.venv-codex/bin/mcp-web-search" --stdio
```

Project config (`.mcp.json` in repo root):
```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "/ABS/PATH/TO/REPO/.venv/bin/mcp-web-search",
      "args": ["--stdio"],
      "env": {
        "SERPER_API_KEY": "${SERPER_API_KEY}",
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

### Codex (CLI / IDE extension)

Prereq (WSL/Linux): install into a virtualenv so the executable exists.

If you used `uv`, it will typically create `.venv/` automatically; in that case the executable will be at:
- `$(pwd)/.venv/bin/mcp-web-search`

If you prefer a dedicated virtualenv for agents/automation in this repo, use `.venv-codex/`:
```bash
python -m venv .venv-codex
.venv-codex/bin/python -m pip install -U pip
.venv-codex/bin/python -m pip install -e .
```

CLI install (stdio):
```bash
codex mcp add kindly-web-search \
  --env SERPER_API_KEY="$SERPER_API_KEY" \
  --env GITHUB_TOKEN="$GITHUB_TOKEN" \
  -- "$(pwd)/.venv/bin/mcp-web-search" --stdio

# If you used `.venv-codex/` instead of `.venv/`, use:
#   -- "$(pwd)/.venv-codex/bin/mcp-web-search" --stdio
```

Manual config (`~/.codex/config.toml`):
```toml
[mcp_servers.kindly-web-search]
command = "/ABS/PATH/TO/REPO/.venv/bin/mcp-web-search"
args = ["--stdio"]

# Prefer keeping secrets out of config files:
env_vars = ["SERPER_API_KEY", "GITHUB_TOKEN"]
```

### Gemini CLI

Gemini uses JSON config under `mcpServers` (project `.gemini/settings.json` or user `~/.gemini/settings.json`).

```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "mcp-web-search",
      "args": ["--stdio"],
      "env": {
        "SERPER_API_KEY": "$SERPER_API_KEY",
        "GITHUB_TOKEN": "$GITHUB_TOKEN"
      }
    }
  }
}
```

### Cursor

Project config (commonly `.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "mcp-web-search",
      "args": ["--stdio"],
      "env": {
        "SERPER_API_KEY": "${SERPER_API_KEY}",
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

### Claude Desktop

Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\\Claude\\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "mcp-web-search",
      "args": ["--stdio"],
      "env": {
        "SERPER_API_KEY": "REPLACE_ME",
        "GITHUB_TOKEN": "REPLACE_ME"
      }
    }
  }
}
```

### GitHub Copilot (VS Code) + Microsoft Copilot (local)

VS Code MCP config is typically `.vscode/mcp.json`. Example (stdio):
```json
{
  "servers": {
    "kindly-web-search": {
      "command": "mcp-web-search",
      "args": ["--stdio"],
      "env": {
        "SERPER_API_KEY": "${env:SERPER_API_KEY}",
        "GITHUB_TOKEN": "${env:GITHUB_TOKEN}"
      }
    }
  }
}
```

For enterprise / hosted scenarios (Copilot Studio / Microsoft Copilot), prefer running this MCP server remotely over **HTTPS Streamable HTTP** and store secrets in the hosting platform (not in client config files).

## Development Environments (Windows + WSL)

This repo is typically developed on Windows, but automation/agents may run in WSL/Linux.

- `.venv/` may contain **Windows** Python. Use it in PyCharm/Windows only.
- `.venv-codex/` is a **Linux/WSL** virtualenv used by automation in this repo. Do not use `.venv/` from WSL.

## Testing

Unit tests (recommended):
```bash
python -m pytest -q
```

Live integration tests:
- Set `RUN_LIVE_TESTS=1` to enable (default: skipped).
- `tests/test_serper_live.py` will load `SERPER_API_KEY` from:
  1) your environment (preferred), otherwise
  2) `tests/.env.test` (gitignored)

## Examples

- `examples/script_run_mcp_tools.py` demonstrates calling the tool functions directly (useful for local debugging).

## Docker

This repo includes a `Dockerfile` for a “no local Python install” path.

Build:
```bash
docker build -t mcp-web-search:local .
```

Run as a stdio MCP server (works with clients that can launch `docker run -i ...`):
```bash
docker run -i --rm \
  -e SERPER_API_KEY \
  -e GITHUB_TOKEN \
  mcp-web-search:local --stdio
```

Run as Streamable HTTP (for gateways / remote clients):
```bash
docker run --rm -p 8000:8000 \
  -e SERPER_API_KEY \
  -e GITHUB_TOKEN \
  -e FASTMCP_HOST=0.0.0.0 \
  -e FASTMCP_PORT=8000 \
  mcp-web-search:local --http
```

Notes:
- Use `-i` for stdio so stdin stays open.
- For clean signal handling, consider `docker run --init ...`.

## Troubleshooting

- **nodriver says it can’t find Chrome/Chromium**: install a Chromium-based browser and/or set `KINDLY_BROWSER_EXECUTABLE_PATH`.
  - Ubuntu/WSL example: `sudo apt-get update && sudo apt-get install -y chromium`
  - Then: `export KINDLY_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium`
- **No `page_content` / empty content**: the site may block automation or require login; try `get_content(url)` directly and inspect the returned Markdown error note.
- **GitHub Issues retrieval fails**: ensure `GITHUB_TOKEN` is set and has permission to read the target repo’s issues.
- **Noisy stdout during PDF conversion**: this repo suppresses third-party PDF conversion prints to keep MCP stdio clean (see `content/arxiv.py`).
