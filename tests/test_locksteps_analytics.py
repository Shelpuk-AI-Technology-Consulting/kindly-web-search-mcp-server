"""Locksteps Connector Analytics stays off unless a key is configured.

The ingest client is the official JSON POST
(https://www.locksteps.ai/docs/claude-mcp). These tests pin the two properties
that matter for an opt-in hook on the tool path: with ``LOCKSTEPS_API_KEY``
unset there is no request, and a failed request cannot change a tool result.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.analytics import (
    API_KEY_ENV,
    CONNECTOR_ID_ENV,
    DEFAULT_CONNECTOR_ID,
    INGEST_URL,
    record_tool_call,
)
from kindly_web_search_mcp_server.models import WebSearchResult


class _InlineThread:
    """Run the ingest target on the caller so a test never opens a socket."""

    def __init__(
        self,
        group: object = None,
        target: object = None,
        name: str | None = None,
        args: tuple = (),
        kwargs: dict | None = None,
        *,
        daemon: bool | None = None,
    ) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


async def test_analytics_is_a_no_op_without_the_api_key() -> None:
    """Leave the network alone when the key is missing or blank."""
    from kindly_web_search_mcp_server.server import get_content, web_search

    def forbid_thread(*args: object, **kwargs: object) -> None:
        raise AssertionError("analytics started a thread without an API key")

    def forbid_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("analytics opened a URL without an API key")

    with patch.dict(os.environ, {}, clear=False), patch(
        "kindly_web_search_mcp_server.analytics.threading.Thread", forbid_thread
    ), patch("kindly_web_search_mcp_server.analytics.urlopen", forbid_urlopen):
        os.environ.pop(API_KEY_ENV, None)
        record_tool_call(tool_name="web_search", phrase="should not be sent")
        os.environ[API_KEY_ENV] = "   "
        record_tool_call(tool_name="web_search", phrase="blank key")

    mocked = [
        WebSearchResult(
            title="T", link="https://example.com", snippet="S", page_content=""
        )
    ]
    with patch.dict(os.environ, {}, clear=False), patch(
        "kindly_web_search_mcp_server.analytics.threading.Thread", forbid_thread
    ), patch(
        "kindly_web_search_mcp_server.analytics.urlopen", forbid_urlopen
    ), patch(
        "kindly_web_search_mcp_server.server.search_web", new_callable=AsyncMock
    ) as mock_search, patch(
        "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
        new_callable=AsyncMock,
    ) as mock_resolve:
        os.environ.pop(API_KEY_ENV, None)
        mock_search.return_value = mocked
        mock_resolve.return_value = "# Title\n\nHello"
        search_out = await web_search("hello", num_results=1)
        content_out = await get_content("https://example.com/docs")

    assert search_out["results"][0]["title"] == "T"
    assert "Hello" in search_out["results"][0]["page_content"]
    assert content_out["url"] == "https://example.com/docs"
    assert "Hello" in content_out["page_content"]


async def test_a_tool_call_posts_one_ingest_event_and_ignores_ingest_failures() -> None:
    """Send the documented event, then still return the tool result if it fails."""
    from kindly_web_search_mcp_server.server import get_content, web_search

    captured: list[tuple[Any, Any]] = []

    def failing_urlopen(request: Any, timeout: Any = None) -> None:
        captured.append((request, timeout))
        raise URLError("timed out")

    mocked = [
        WebSearchResult(
            title="T", link="https://example.com", snippet="S", page_content=""
        )
    ]
    phrase = "q" * 2500
    with patch.dict(
        os.environ,
        {API_KEY_ENV: "dsn_test_key", CONNECTOR_ID_ENV: ""},
        clear=False,
    ), patch(
        "kindly_web_search_mcp_server.analytics.threading.Thread", _InlineThread
    ), patch(
        "kindly_web_search_mcp_server.analytics.urlopen", failing_urlopen
    ), patch(
        "kindly_web_search_mcp_server.server.search_web", new_callable=AsyncMock
    ) as mock_search, patch(
        "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
        new_callable=AsyncMock,
    ) as mock_resolve:
        mock_search.return_value = mocked
        mock_resolve.return_value = "# Title\n\nHello"
        search_out = await web_search(phrase, num_results=1)
        with patch.dict(os.environ, {CONNECTOR_ID_ENV: "my-mcp-server"}, clear=False):
            content_out = await get_content("https://example.com/docs")
        mock_search.return_value = []
        empty_out = await web_search("nothing")
        mock_search.side_effect = RuntimeError("provider down")
        with pytest.raises(RuntimeError, match="provider down"):
            await web_search("broken query")

    assert search_out["results"][0]["link"] == "https://example.com"
    assert "Hello" in content_out["page_content"]
    assert empty_out["results"] == []
    assert len(captured) == 4
    search_request, search_timeout = captured[0]
    content_request, content_timeout = captured[1]
    assert search_timeout == 2
    assert content_timeout == 2

    search_body = _event(search_request)
    assert search_request.get_method() == "POST"
    assert search_request.full_url == INGEST_URL
    assert search_request.get_header("Authorization") == "Bearer dsn_test_key"
    assert search_request.get_header("Content-type") == "application/json"
    assert "dsn_test_key" not in json.dumps(search_body)
    assert search_body["connector_id"] == DEFAULT_CONNECTOR_ID
    assert search_body["platform"] == "mcp"
    assert search_body["tool_name"] == "web_search"
    assert search_body["phrase"] == "q" * 2000
    assert search_body["outcome"] == "success"
    assert search_body["result_count"] == 1
    assert isinstance(search_body["latency_ms"], int)
    assert search_body["event_id"]
    assert "T" in search_body["ts"]

    content_body = _event(content_request)
    assert content_body["connector_id"] == "my-mcp-server"
    assert content_body["tool_name"] == "get_content"
    assert content_body["phrase"] == "https://example.com/docs"
    assert content_body["outcome"] == "success"
    assert "result_count" not in content_body

    empty_body = _event(captured[2][0])
    assert empty_body["phrase"] == "nothing"
    assert empty_body["outcome"] == "no_result"
    assert empty_body["result_count"] == 0

    error_body = _event(captured[3][0])
    assert error_body["phrase"] == "broken query"
    assert error_body["outcome"] == "error"
    assert "result_count" not in error_body


def _event(request: Any) -> dict[str, Any]:
    return json.loads(request.data.decode())
