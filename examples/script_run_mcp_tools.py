from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio


def ensure_src_on_sys_path(repo_root: Path) -> None:
    """Allow running this script directly (e.g., from PyCharm) without installing the package."""
    src_dir = repo_root / "src"
    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def parse_bool(raw: str, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def normalize_call_tool_result(result: object) -> object:
    """
    `FastMCP.call_tool()` may return:
    - a JSON-serializable dict, or
    - a list of MCP ContentBlocks (commonly TextContent containing JSON text).
    """
    if isinstance(result, dict):
        return result

    if isinstance(result, list):
        texts: list[str] = []
        for item in result:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                texts.append(text)
            else:
                texts.append(str(item))

        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except Exception:
                return {"content": texts[0]}

        return {"content": texts}

    return {"result": str(result)}


async def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    ensure_src_on_sys_path(repo_root)

    # Environment variables are expected to be configured by the IDE/run configuration.
    from kindly_web_search_mcp_server.server import mcp

    query = sys.argv[1] if len(sys.argv) > 1 else "OpenCV affine image transformation"
    num_results = int(os.environ.get("NUM_RESULTS", "5"))
    return_full_pages = parse_bool(os.environ.get("RETURN_FULL_PAGES", "true"), default=True)

    # This calls the MCP tool handler directly (no MCP host required).
    result = await mcp.call_tool(
        "web_search",
        arguments={
            "query": query,
            "num_results": num_results,
            "return_full_pages": return_full_pages,
        },
    )

    normalized = normalize_call_tool_result(result)
    print(json.dumps(normalized, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    # Run example:
    #   PYTHONPATH=src ./.venv-codex/bin/python examples/script_run_mcp_tools.py "error 1004 freeze panes"
    anyio.run(main)
