"""Analytics does nothing unless LOCKSTEPS_API_KEY is set."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.analytics import record_tool_call


def test_record_tool_call_is_a_no_op_without_the_api_key() -> None:
    """Do not start a thread or open a URL when the key is missing or blank."""

    def forbid(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("analytics ran without an API key")

    with patch.dict(os.environ, {}, clear=False), patch(
        "kindly_web_search_mcp_server.analytics.threading.Thread", forbid
    ), patch("kindly_web_search_mcp_server.analytics.urlopen", forbid):
        os.environ.pop("LOCKSTEPS_API_KEY", None)
        record_tool_call("web_search", "hello")
        os.environ["LOCKSTEPS_API_KEY"] = "   "
        record_tool_call("get_content", "https://example.com")
