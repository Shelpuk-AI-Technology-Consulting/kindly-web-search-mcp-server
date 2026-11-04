"""Unit tests for the Firecrawl search provider.

Firecrawl is last in the selection order. The layout mirrors
``test_serply_unit.py``: the request shape and the parsing live here, and the
transport-level failures (401, 429, a non-JSON body, a wrong-shaped JSON body and
a timeout) live in ``test_search_provider_error_paths.py``, which drives every
provider from one table.

**Written in pytest style**, like ``test_serply_unit.py`` and for the reason
section 3.1 of ``.system_design/TEST_SUITE.md`` records.

**Only search is requested.** The request cases pin the whole JSON body, so a
later change that starts sending ``scrapeOptions`` (and with them a second,
provider-side page fetch) has to be deliberate.
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
from kindly_web_search_mcp_server.search.firecrawl import (
    MAX_LIMIT,
    FirecrawlConfigError,
    FirecrawlError,
    search_firecrawl,
)

#: Captured before any case rebinds :class:`httpx.AsyncClient`, so the recording
#: double always subclasses the real client rather than an earlier double.
REAL_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "fc-test"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Firecrawl a dummy credential for the duration of one test.

    Args:
        monkeypatch: pytest's environment patcher, which restores the previous
            value when the test ends.
    """
    monkeypatch.setenv("FIRECRAWL_API_KEY", API_KEY)


async def run_search(
    payload: Any,
    *,
    num_results: int = 3,
    query: str = "q",
    seen: list[httpx.Request] | None = None,
) -> list[WebSearchResult]:
    """Run ``search_firecrawl`` against a mocked response.

    Args:
        payload: JSON body the mocked Firecrawl API returns.
        num_results: Value forwarded to ``search_firecrawl``.
        query: Query forwarded to ``search_firecrawl``.
        seen: When given, receives each outgoing request.

    Returns:
        The parsed results.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with ``payload``.

        Args:
            request: The outgoing request.

        Returns:
            A 200 response carrying ``payload``.
        """
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await search_firecrawl(query, num_results=num_results, http_client=client)


def results_payload(count: int) -> dict[str, Any]:
    """Build a response carrying ``count`` well-formed results.

    Args:
        count: How many results the payload holds.

    Returns:
        A Firecrawl-shaped response body whose results are titled ``Result 0``
        onward.
    """
    return {
        "success": True,
        "data": {
            "web": [
                {"title": f"Result {i}", "url": f"https://example.org/{i}", "description": "s"}
                for i in range(count)
            ]
        },
    }


async def test_parses_documented_results(configured: None) -> None:
    """Parse the documented success shape into the server's result model"""
    results = await run_search(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://www.python-httpx.org/async/",
                        "title": "Async Support - HTTPX",
                        "description": "HTTPX offers an optional async client.",
                        "position": 1,
                    }
                ]
            },
            "creditsUsed": 2,
            "id": "00000000-0000-0000-0000-000000000000",
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
    """Send one POST with the key in a bearer header and the whole body pinned

    The body is compared in full, so an extra field (``scrapeOptions``, a
    ``sources`` list) fails here as surely as a missing ``origin`` does.
    """
    seen: list[httpx.Request] = []
    await run_search({"data": {"web": []}}, num_results=4, query="httpx", seen=seen)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://api.firecrawl.dev/v2/search"
    assert seen[0].headers.get("authorization") == f"Bearer {API_KEY}"
    assert json.loads(seen[0].content) == {
        "query": "httpx",
        "limit": 4,
        "origin": "kindly-web-search-mcp-server",
    }


async def test_clamps_a_large_limit_to_the_documented_maximum(configured: None) -> None:
    """Send at most 100, because the API answers a larger `limit` with HTTP 400

    You.com and Sofya clamp for the same reason. Without the clamp a large
    ``num_results`` would fail the whole search instead of returning fewer
    results.
    """
    seen: list[httpx.Request] = []
    await run_search({"data": {"web": []}}, num_results=500, seen=seen)

    assert json.loads(seen[0].content)["limit"] == MAX_LIMIT == 100


async def test_returns_the_first_num_results_in_order(configured: None) -> None:
    """Stop at the caller's bound, keeping the API's ranking"""
    results = await run_search(results_payload(3), num_results=2)

    assert [result.title for result in results] == ["Result 0", "Result 1"]


async def test_keeps_a_result_that_has_no_description(configured: None) -> None:
    """Keep a usable link even with no snippet, since page_content is fetched later"""
    results = await run_search(
        {"data": {"web": [{"title": "Result", "url": "https://example.org"}]}}
    )

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
        {
            "data": {
                "web": [
                    {"title": "Result", "url": "https://example.org", "description": description}
                ]
            }
        }
    )

    assert results[0].snippet == ""


async def test_skips_an_entry_whose_title_is_not_a_string(configured: None) -> None:
    """Drop a result with a usable url but no usable title

    A good ``url`` beside the bad title is what makes the ``title`` conjunct
    observable; in the all-unusable payload below the bad-title entry also has a
    bad ``url``, so the ``url`` conjunct would still reject it with the ``title``
    conjunct deleted.
    """
    results = await run_search(
        {
            "data": {
                "web": [
                    {"title": 7, "url": "https://odd-title.example/", "description": "s"},
                    {"title": "Good", "url": "https://good.example/", "description": "ok"},
                ]
            }
        }
    )

    assert [result.title for result in results] == ["Good"]


async def test_raises_when_no_returned_result_is_usable(configured: None) -> None:
    """Fail loudly instead of returning nothing when the schema does not match"""
    with pytest.raises(FirecrawlError, match=r"returned 3 result\(s\) but none could be parsed"):
        await run_search(
            {
                "data": {
                    "web": [
                        {"headline": "no title or url"},
                        "not an object",
                        {"title": "Result", "url": 123},
                    ]
                }
            }
        )


async def test_returns_empty_when_the_api_found_nothing(configured: None) -> None:
    """Return no results, without error, when the query genuinely matched nothing"""
    assert await run_search({"success": True, "data": {"web": []}}) == []


async def test_returns_empty_when_the_web_key_is_absent(configured: None) -> None:
    """Read a `data` object with no `web` key as zero hits, not a schema change"""
    assert await run_search({"success": True, "data": {}}) == []


@pytest.mark.parametrize(
    "body",
    [{"success": True}, {"success": True, "data": None}, {"success": True, "data": []}],
    ids=["absent", "null", "list"],
)
async def test_raises_when_the_data_object_is_missing(configured: None, body: Any) -> None:
    """A response without a `data` object is a schema change, not zero hits

    The list case is the shape the older v1 endpoint returned, which is the
    likeliest way this provider meets the wrong envelope.

    Args:
        configured: Fixture providing the dummy credential.
        body: The response body without a usable ``data`` object.
    """
    with pytest.raises(FirecrawlError, match="missing `data` object"):
        await run_search(body)


async def test_raises_when_web_is_not_a_list(configured: None) -> None:
    """Reject a `data.web` value that is not a list

    Driven with ``null``. With the guard removed, ``null`` is not iterable, so
    the provider raises ``TypeError`` and the case fails on the class.
    """
    with pytest.raises(FirecrawlError, match="`data.web` is not a list"):
        await run_search({"data": {"web": None}})


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
    """Report a missing FIRECRAWL_API_KEY as configuration, before any request

    The exact class is asserted because ``FirecrawlConfigError`` subclasses
    ``FirecrawlError``, and a base-class check would also accept a parsing
    failure.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    with pytest.raises(FirecrawlConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is FirecrawlConfigError
    assert seen == []


async def test_a_whitespace_only_key_raises_config_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a key of only whitespace as missing rather than sending it

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("FIRECRAWL_API_KEY", "   ")
    seen: list[httpx.Request] = []

    with pytest.raises(FirecrawlConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is FirecrawlConfigError
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
        """Record the request and answer with an empty result set.

        Args:
            request: The outgoing request.

        Returns:
            A 200 response with no results.
        """
        seen.append(request)
        return httpx.Response(200, json={"data": {"web": []}})

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

    await search_firecrawl("q", num_results=1)

    assert seen[0].extensions["timeout"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
