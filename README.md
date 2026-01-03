# Kindly Web Search MCP Server

**Web search + robust content retrieval for AI coding tools.**

![Kindly Web Search](assets/kindly_header.png)

## Why do we need another web search MCP server?

Picture this: You're debugging a cryptic error in Google Cloud Batch with GPU instances. Your AI coding assistant searches the web and finds the *perfect* StackOverflow thread. Great, right? Not quite. Here's what most web search MCP servers give your AI:

```json
{
  "title": "GCP Cloud Batch fails with the GPU instance template",
  "url": "https://stackoverflow.com/questions/76546453/...",
  "snippet": "I am trying to run a GCP Cloud Batch job with K80 GPU. The job runs for ~30 min. and then fails..."
}
```

The question is there, but **where are the answers?** Where are the solutions that other developers tried? The workarounds? The "this worked for me" comments?

They're not there. Your AI now has to make a second call to scrape the page. Sometimes it does, sometimes it doesn't. And even when it does, most scrapers return either incomplete content or the entire webpage with navigation panels, ads, and other noise that wastes tokens and confuses the AI.

### The Real Problem

At [Shelpuk AI Technology Consulting](https://shelpuk.com), we build custom AI products under a fixed-price model. Development efficiency isn't just nice to have - it's the foundation of our business. We've been using AI coding assistants since 2023 (GitHub Copilot, Cursor, Windsurf, Claude Code, Codex), and we noticed something frustrating:

**When we developers face a complex bug, we don't just want to find a URL - we want to find the conversation.** We want to see what others tried, what worked, what didn't, and why. We want the GitHub Issue with all the comments. We want the StackOverflow thread with upvoted answers and follow-up discussions. We want the arXiv paper content, not just its abstract.

Existing web search MCP servers are basically wrappers around search APIs. They're great at *finding* content, but terrible at *delivering* it in a way that's useful for AI coding assistants.

### What Kindly Does Differently

We built Kindly Web Search because we needed our AI assistants to work the way *we* work. When searching for solutions, Kindly:

✅ **Integrates directly with APIs** for StackExchange, GitHub Issues, arXiv, and Wikipedia - presenting content in LLM-optimized formats with proper structure

✅ **Returns the full conversation** in a single call: questions, answers, comments, reactions, and metadata

✅ **Parses any webpage in real-time** using a headless browser for cutting-edge issues that were literally posted yesterday

✅ **Passes all useful content to the LLM immediately** - no need for a second scraping call

✅ **Supports multiple search providers** (Serper and Tavily) with intelligent fallback

Now, when Claude Code or Codex searches for that GPU batch error, it gets the question *and* the answers. The code snippets. The "this fixed it for me" comments. Everything it needs to help you solve the problem - **in one call**.

## One MCP Server to Rule Them All

Kindly eliminates the need for:

✅ Generic web search MCP servers

✅ StackOverflow MCP servers

✅ Web scraping MCP servers (Playwright, Puppeteer, etc.)

It also significantly reduces reliance on GitHub MCP servers by providing structured Issue content through intelligent extraction.

Kindly has been our daily companion in production work for months, saving us countless hours and improving the effectiveness of our AI coding assistants. We're excited to share it with the community!

**Tools**
- `web_search(query, num_results=3)` → top results with `title`, `link`, `snippet`, and `page_content` (Markdown, best-effort).
- `get_content(url)` → `page_content` (Markdown, best-effort).

Search uses **Serper** (primary, if configured) or **Tavily**, and page extraction uses a local Chromium-based browser via `nodriver`.

## Requirements
- A search API key: `SERPER_API_KEY` **or** `TAVILY_API_KEY`
- A Chromium-based browser installed on the same machine running the MCP client (Chrome/Chromium/Edge/Brave)
- Highly recommended: `GITHUB_TOKEN` (renders GitHub Issues in a much more LLM-friendly format: question + answers/comments + reactions/metadata; fewer rate limits)

`GITHUB_TOKEN` can be read-only and limited to public repositories to avoid security/privacy concerns.

## Quickstart

### 1) Install `uvx`
macOS / Linux:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

Restart your terminal so `uvx` is available.

Verify:
```bash
uvx --version
```

### 2) Install Chromium (needed by `nodriver`)
macOS (Homebrew):
```bash
brew install --cask chromium
```

Windows:
- Install **Chrome** or **Edge** normally.
- Optional (PowerShell): `winget install --id Google.Chrome -e`

Linux (Ubuntu/Debian):
```bash
sudo apt-get update
sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
```

Linux (Fedora):
```bash
sudo dnf install -y chromium
```

### 3) Set your key(s)
Set **one** search key (required):

macOS / Linux:
```bash
export SERPER_API_KEY="..."
# or: export TAVILY_API_KEY="..."
```

Windows (PowerShell):
```powershell
$env:SERPER_API_KEY="..."
# or: $env:TAVILY_API_KEY="..."
```

Optional:
```bash
export GITHUB_TOKEN="..."
```

Windows (PowerShell):
```powershell
$env:GITHUB_TOKEN="..."
```

If you want the best results when searching/debugging, set `GITHUB_TOKEN`: it lets the server render GitHub Issues/PRs with structure (question, each answer/comment, reactions, and metadata). Use a read-only token limited to public repositories.

### 4) Add the MCP server to your client
If you don’t have `GITHUB_TOKEN` yet, you can omit it, but GitHub Issues/PRs will be extracted with less structure.

**Codex (recommended: CLI)**
macOS / Linux:
```bash
codex mcp add kindly-web-search \
  --env SERPER_API_KEY="$SERPER_API_KEY" \
  --env GITHUB_TOKEN="$GITHUB_TOKEN" \
  -- uvx --from git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server \
  kindly-web-search-mcp-server start-mcp-server
```

Windows (PowerShell):
```powershell
codex mcp add kindly-web-search `
  --env SERPER_API_KEY="$env:SERPER_API_KEY" `
  --env GITHUB_TOKEN="$env:GITHUB_TOKEN" `
  -- uvx --from git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server `
  kindly-web-search-mcp-server start-mcp-server
```

**Claude Code (recommended: CLI)**
macOS / Linux:
```bash
claude mcp add --transport stdio kindly-web-search \
  --env SERPER_API_KEY="$SERPER_API_KEY" \
  --env GITHUB_TOKEN="$GITHUB_TOKEN" \
  -- uvx --from git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server \
  kindly-web-search-mcp-server start-mcp-server
```

Windows (PowerShell):
```powershell
claude mcp add --transport stdio kindly-web-search `
  --env SERPER_API_KEY="$env:SERPER_API_KEY" `
  --env GITHUB_TOKEN="$env:GITHUB_TOKEN" `
  -- uvx --from git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server `
  kindly-web-search-mcp-server start-mcp-server
```

Only set **one** search key. If you use Tavily instead of Serper, use `TAVILY_API_KEY` *instead of* `SERPER_API_KEY`.

## Details

### The server command (for any MCP client)
```bash
uvx --from git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server \
  kindly-web-search-mcp-server start-mcp-server
```

### Browser setup (when auto-detection fails)
If the server can’t find a browser, set `KINDLY_BROWSER_EXECUTABLE_PATH` to your browser binary.

macOS (Homebrew Chromium):
```bash
export KINDLY_BROWSER_EXECUTABLE_PATH="/Applications/Chromium.app/Contents/MacOS/Chromium"
```

Linux:
```bash
export KINDLY_BROWSER_EXECUTABLE_PATH="$(command -v chromium || command -v chromium-browser)"
```

Windows (PowerShell):
```powershell
$env:KINDLY_BROWSER_EXECUTABLE_PATH="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
```

### Client configuration (file-based)
Notes:
- All examples run the same server command. Use `TAVILY_API_KEY` instead of `SERPER_API_KEY` if you’re using Tavily.
- `GITHUB_TOKEN` is highly recommended (GitHub Issues/PRs become much more structured/usable).

#### Codex (alternative: config file)
Edit `~/.codex/config.toml`:
```toml
[mcp_servers.kindly-web-search]
command = "uvx"
args = ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"]
env_vars = ["SERPER_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "KINDLY_BROWSER_EXECUTABLE_PATH"]
startup_timeout_sec = 60.0
```

#### Claude Code (alternative: `.mcp.json`)
Create/edit `.mcp.json` (project scope) or `~/.config/claude-code/.mcp.json` (user scope):
```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"],
      "env": { "SERPER_API_KEY": "${SERPER_API_KEY}", "GITHUB_TOKEN": "${GITHUB_TOKEN}", "KINDLY_BROWSER_EXECUTABLE_PATH": "${KINDLY_BROWSER_EXECUTABLE_PATH}" }
    }
  }
}
```

#### Cursor
Create `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "kindly-web-search": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"],
      "env": { "SERPER_API_KEY": "${env:SERPER_API_KEY}", "GITHUB_TOKEN": "${env:GITHUB_TOKEN}", "KINDLY_BROWSER_EXECUTABLE_PATH": "${env:KINDLY_BROWSER_EXECUTABLE_PATH}" }
    }
  }
}
```

#### Gemini CLI
Edit `~/.gemini/settings.json` (or `.gemini/settings.json` in a project):
```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"],
      "env": { "SERPER_API_KEY": "$SERPER_API_KEY", "GITHUB_TOKEN": "$GITHUB_TOKEN", "KINDLY_BROWSER_EXECUTABLE_PATH": "$KINDLY_BROWSER_EXECUTABLE_PATH" }
    }
  }
}
```

#### Claude Desktop
Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\\Claude\\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "kindly-web-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"],
      "env": { "SERPER_API_KEY": "PASTE_SERPER_OR_USE_TAVILY_API_KEY", "GITHUB_TOKEN": "PASTE_GITHUB_TOKEN", "KINDLY_BROWSER_EXECUTABLE_PATH": "PASTE_IF_NEEDED" }
    }
  }
}
```

#### GitHub Copilot / Microsoft Copilot (VS Code)
Create `.vscode/mcp.json`:
```json
{
  "servers": {
    "kindly-web-search": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Shelpuk-AI-Technology-Consulting/kindly-web-search-mcp-server", "kindly-web-search-mcp-server", "start-mcp-server"],
      "env": {
        "SERPER_API_KEY": "${input:serper-api-key}",
        "TAVILY_API_KEY": "${input:tavily-api-key}",
        "GITHUB_TOKEN": "${input:github-token}",
        "KINDLY_BROWSER_EXECUTABLE_PATH": "${input:browser-path}"
      }
    }
  },
  "inputs": [
    { "id": "serper-api-key", "type": "promptString", "description": "Serper API key (leave empty if using Tavily)" },
    { "id": "tavily-api-key", "type": "promptString", "description": "Tavily API key (leave empty if using Serper)" },
    { "id": "github-token", "type": "promptString", "description": "GitHub token (recommended)" },
    { "id": "browser-path", "type": "promptString", "description": "Browser binary path (only if needed)" }
  ]
}
```

### Troubleshooting (common)
- “No Chromium-based browser executable found”: install Chrome/Chromium/Edge and (if needed) set `KINDLY_BROWSER_EXECUTABLE_PATH`.
- “web_search fails: no provider key”: set `SERPER_API_KEY` or `TAVILY_API_KEY` in your client config.
- “GitHub Issues/PRs look unstructured”: set `GITHUB_TOKEN` (read-only, public-only is fine).
- Some sites block automation: `page_content` may contain a short error note; try `get_content(url)` to see the exact failure.

### Security notes
- Don’t commit API keys.
- Prefer env-var expansion in config files (when supported) instead of hardcoding secrets.
