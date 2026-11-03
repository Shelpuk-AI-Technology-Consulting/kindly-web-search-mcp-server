"""Unit tests for the Keenable search provider.

Keenable is last in the selection order. The layout mirrors
``test_serply_unit.py``: the request shape and the parsing live here, and the
transport-level failures (401, 429, a non-JSON body, a wrong-shaped JSON body and
a timeout) live in ``test_search_provider_error_paths.py``, which drives every
provider from one table.

**Written in pytest style**, like ``test_serply_unit.py`` and for the reason
section 3.1 of ``.system_design/TEST_SUITE.md`` records.

Keenable takes a JSON ``POST`` to ``/v1/search`` with ``{"query", "max_results"}``
and the key in ``X-API-Key``. ``description`` is preferred as the snippet; a
truncated ``snippet`` excerpt is the fallback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search.keenable import (
    SNIPPET_MAX_CHARS,
    KeenableConfigError,
    KeenableError,
    search_keenable,
)

#: Captured before any case rebinds :class:`httpx.AsyncClient`, so the recording
#: double always subclasses the real client rather than an earlier double.
REAL_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "keenable_test"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Keenable a dummy credential for the duration of one test.

    Args:
        monkeypatch: pytest's environment patcher, which restores the previous
            value when the test ends.
    """
    monkeypatch.setenv("KEENABLE_API_KEY", API_KEY)


async def run_search(
    payload: dict[str, Any],
    *,
    num_results: int = 3,
    query: str = "q",
    seen: list[httpx.Request] | None = None,
) -> list[WebSearchResult]:
    """Run ``search_keenable`` against a mocked response.

    Args:
        payload: JSON body the mocked Keenable API returns.
        num_results: Value forwarded to ``search_keenable``.
        query: Query forwarded to ``search_keenable``.
        seen: When given, receives each outgoing request.

    Returns:
        The parsed results.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await search_keenable(query, num_results=num_results, http_client=client)


def results_payload(count: int) -> dict[str, Any]:
    """Build a response carrying ``count`` well-formed results.

    Args:
        count: How many results the payload holds.

    Returns:
        A Keenable-shaped response body whose results are titled ``Result 0`` onward.
    """
    return {
        "results": [
            {"title": f"Result {i}", "url": f"https://example.org/{i}", "description": "s"}
            for i in range(count)
        ]
    }


async def test_parses_documented_results(configured: None) -> None:
    """Parse the documented success shape into the server's result model"""
    results = await run_search(
        {
            "results": [
                {
                    "title": "Async Support - HTTPX",
                    "url": "https://www.python-httpx.org/async/",
                    "description": "HTTPX offers an optional async client.",
                    "position": 1,
                    "realPosition": 1,
                    "result_type": "organic",
                    "metadata": {"display_url": "www.python-httpx.org"},
                }
            ],
            "total": 1,
            "query": "httpx",
        },
        num_results=1,
    )

    assert len(results) == 1
    assert results[0].title == "Async Support - HTTPX"
    assert results[0].link == "https://www.python-httpx.org/async/"
    assert results[0].snippet == "HTTPX offers an optional async client."
    # `page_content` is filled in later by the MCP tool, never by the provider.
    assert results[0].page_content == ""


async def test_sends_the_documented_request(configured: None) -> None:
    """Send one JSON POST carrying the query in the body and the key in a header"""
    seen: list[httpx.Request] = []
    await run_search({"results": []}, num_results=4, query="c++ & rust", seen=seen)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://api.keenable.ai/v1/search"
    assert seen[0].headers.get("x-api-key") == API_KEY
    assert seen[0].headers.get("x-keenable-title") == "kindly-web-search-mcp-server"
    assert json.loads(seen[0].content) == {"query": "c++ & rust", "max_results": 4}


async def test_falls_back_to_a_truncated_snippet_without_description(
    configured: None,
) -> None:
    """Use the page excerpt, capped, when no description is present"""
    results = await run_search(
        {
            "results": [
                {"title": "A", "url": "https://a.example/", "description": "d", "snippet": "s"},
                {"title": "B", "url": "https://b.example/", "snippet": "x" * 900},
            ]
        },
        num_results=5,
    )

    assert [result.snippet for result in results] == ["d", "x" * SNIPPET_MAX_CHARS]


async def test_returns_the_first_num_results_in_order(configured: None) -> None:
    """Stop at the caller's bound, keeping the API's ranking"""
    results = await run_search(results_payload(3), num_results=2)

    assert [result.title for result in results] == ["Result 0", "Result 1"]


async def test_keeps_a_result_that_has_no_description(configured: None) -> None:
    """Keep a usable link even with no snippet, since page_content is fetched later"""
    results = await run_search({"results": [{"title": "Result", "url": "https://example.org"}]})

    assert [(result.title, result.snippet) for result in results] == [("Result", "")]


@pytest.mark.parametrize("description", [None, 123], ids=["null", "number"])
async def test_treats_a_non_string_description_as_absent(
    configured: None, description: object
) -> None:
    """Store an empty snippet rather than a malformed `description`

    ``123`` is the value that separates a type check from a truthiness test:
    ``description or ""`` treats ``null`` as absent too, so ``null`` alone cannot
    tell the two apart.

    Args:
        configured: Fixture providing the dummy credential.
        description: The malformed value the API returns.
    """
    results = await run_search(
        {"results": [{"title": "Result", "url": "https://example.org", "description": description}]}
    )

    assert results[0].snippet == ""


async def test_skips_an_entry_whose_title_is_not_a_string(configured: None) -> None:
    """Drop a result with a usable link but no usable title

    A good ``link`` beside the bad title is what makes the ``title`` conjunct
    observable; in the all-unusable payload below the bad-title entry also has a
    bad ``link``, so the ``link`` conjunct would still reject it with the
    ``title`` conjunct deleted.
    """
    results = await run_search(
        {
            "results": [
                {"title": 7, "url": "https://odd-title.example/", "description": "s"},
                {"title": "Good", "url": "https://good.example/", "description": "ok"},
            ]
        }
    )

    assert [result.title for result in results] == ["Good"]


async def test_raises_when_no_returned_result_is_usable(configured: None) -> None:
    """Fail loudly instead of returning nothing when the schema does not match"""
    with pytest.raises(KeenableError, match=r"returned 3 result\(s\) but none could be parsed"):
        await run_search(
            {
                "results": [
                    {"headline": "no title or link"},
                    "not an object",
                    {"title": "Result", "url": 123},
                ]
            }
        )


async def test_returns_empty_when_the_api_found_nothing(configured: None) -> None:
    """Return no results, without error, when the query genuinely matched nothing"""
    assert await run_search({"results": [], "total": 0}) == []


async def test_raises_when_the_results_list_is_missing(configured: None) -> None:
    """A response without a `results` list is a schema change, not zero hits"""
    with pytest.raises(KeenableError, match="missing `results` list"):
        await run_search({"query": "q", "total": 0})


async def test_raises_when_results_is_not_a_list(configured: None) -> None:
    """Reject a `results` value that is not a list

    Driven with ``null``. With the guard removed, ``null`` is not iterable, so
    the provider raises ``TypeError`` and the case fails on the class. An object
    or a string would still be iterated, every element dropped by the item guard,
    and the none-parsed ``KeenableError`` raised instead -- a removal this case
    would then catch only through its message match.
    """
    with pytest.raises(KeenableError, match="missing `results` list"):
        await run_search({"results": None})


@pytest.mark.parametrize("query", ["", "   "], ids=["empty", "whitespace"])
async def test_a_blank_query_returns_no_results_without_a_request(
    configured: None, query: str
) -> None:
    """Answer a blank query locally instead of spending a request on it

    Args:
        configured: Fixture providing the dummy credential.
        query: The blank query.
    """
    seen: list[httpx.Request] = []

    assert await run_search(results_payload(1), query=query, seen=seen) == []
    assert seen == []


@pytest.mark.parametrize("num_results", [0, -1])
async def test_a_non_positive_num_results_returns_no_results_without_a_request(
    configured: None, num_results: int
) -> None:
    """Answer a request for no results locally instead of sending it

    Args:
        configured: Fixture providing the dummy credential.
        num_results: The non-positive bound.
    """
    seen: list[httpx.Request] = []

    assert await run_search(results_payload(1), num_results=num_results, seen=seen) == []
    assert seen == []


async def test_an_unset_key_raises_config_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report a missing KEENABLE_API_KEY as configuration, before any request

    The exact class is asserted because ``KeenableConfigError`` subclasses
    ``KeenableError``, and a base-class check would also accept a parsing failure.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    with pytest.raises(KeenableConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is KeenableConfigError
    assert seen == []


async def test_a_whitespace_only_key_raises_config_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a key of only whitespace as missing rather than sending it

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("KEENABLE_API_KEY", "   ")
    seen: list[httpx.Request] = []

    with pytest.raises(KeenableConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is KeenableConfigError
    assert seen == []


async def test_the_default_client_arms_a_30_second_timeout(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound the request when the caller supplies no client

    Read from the outgoing request's ``timeout`` extension, which is what httpx
    applies, rather than from the constructor's arguments.

    Args:
        configured: Fixture providing the dummy credential.
        monkeypatch: pytest's patcher, which restores :class:`httpx.AsyncClient`.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    class _RecordingClient(REAL_ASYNC_CLIENT):  # type: ignore[valid-type,misc]
        """An ``AsyncClient`` whose transport is always the recording double."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """Force the recording transport while keeping every other argument.

            Args:
                *args: Positional arguments forwarded to :class:`httpx.AsyncClient`.
                **kwargs: Keyword arguments forwarded likewise, with ``transport``
                    replaced.
            """
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)

    await search_keenable("q", num_results=1)

    assert seen[0].extensions["timeout"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
