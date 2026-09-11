"""Unit tests for the Serply search provider.

Serply is seventh and last in the selection order. The layout mirrors
``test_serpbase_unit.py``: the request shape and the parsing live here, and the
transport-level failures (401, 429, a non-JSON body, a wrong-shaped JSON body and
a timeout) live in ``test_search_provider_error_paths.py``, which drives all
seven providers from one table.

**Written in pytest style**, like ``test_serpbase_unit.py`` and for the reason
section 3.1 of ``.system_design/TEST_SUITE.md`` records:
``scripts/check_plan_dag.py`` rejects a new old-style module that no migration
batch claims, and enlarging a batch to convert a file written after the decision
to stop writing them is worse than writing it in the target style.

**The query travels in the URL path, not in a query string.** Serply's reference
and its own example code build ``/v1/search/`` followed by the URL-encoded
``q=...&num=...``. The request cases pin that form, so moving to a query string
-- which the API may answer but does not document -- has to be deliberate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search.serply import (
    SerplyConfigError,
    SerplyError,
    search_serply,
)

#: Captured before any case rebinds :class:`httpx.AsyncClient`, so the recording
#: double always subclasses the real client rather than an earlier double.
REAL_ASYNC_CLIENT = httpx.AsyncClient

API_KEY = "serply_test"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Serply a dummy credential for the duration of one test.

    Args:
        monkeypatch: pytest's environment patcher, which restores the previous
            value when the test ends.
    """
    monkeypatch.setenv("SERPLY_API_KEY", API_KEY)


async def run_search(
    payload: dict[str, Any],
    *,
    num_results: int = 3,
    query: str = "q",
    seen: list[httpx.Request] | None = None,
) -> list[WebSearchResult]:
    """Run ``search_serply`` against a mocked response.

    Args:
        payload: JSON body the mocked Serply API returns.
        num_results: Value forwarded to ``search_serply``.
        query: Query forwarded to ``search_serply``.
        seen: When given, receives each outgoing request.

    Returns:
        The parsed results.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await search_serply(query, num_results=num_results, http_client=client)


def results_payload(count: int) -> dict[str, Any]:
    """Build a response carrying ``count`` well-formed results.

    Args:
        count: How many results the payload holds.

    Returns:
        A Serply-shaped response body whose results are titled ``Result 0`` onward.
    """
    return {
        "results": [
            {"title": f"Result {i}", "link": f"https://example.org/{i}", "description": "s"}
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
                    "link": "https://www.python-httpx.org/async/",
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
    """Send one GET carrying the query in the path and the key in a header

    The whole URL is compared, so a query string appended beside the path form
    fails here as surely as a different host or path does.
    """
    seen: list[httpx.Request] = []
    await run_search({"results": []}, num_results=4, query="httpx", seen=seen)

    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://api.serply.io/v1/search/q=httpx&num=4"
    assert seen[0].headers.get("x-api-key") == API_KEY


@pytest.mark.parametrize(
    ("query", "encoded"),
    [
        ("c++ & rust", "q=c%2B%2B+%26+rust"),
        ("a/b", "q=a%2Fb"),
        ("what?", "q=what%3F"),
        ("c# lang", "q=c%23+lang"),
        ("héllo", "q=h%C3%A9llo"),
    ],
    ids=["plus-and-ampersand", "slash", "question-mark", "hash", "non-ascii"],
)
async def test_url_encodes_the_query_into_the_path(
    configured: None, query: str, encoded: str
) -> None:
    """Encode reserved characters so a query cannot break the path's parameters

    Unencoded, ``&`` would start a new parameter, ``+`` would read as a space,
    ``/`` would add a path segment, and ``?`` or ``#`` would end the path early.
    Non-ASCII text has to arrive as UTF-8 percent-escapes.

    Args:
        configured: Fixture providing the dummy credential.
        query: The query as the caller wrote it.
        encoded: The ``q`` parameter as it must appear in the path.
    """
    seen: list[httpx.Request] = []
    await run_search({"results": []}, query=query, seen=seen)

    assert str(seen[0].url) == f"https://api.serply.io/v1/search/{encoded}&num=3"


async def test_forwards_a_large_num_unchanged(configured: None) -> None:
    """Send the caller's `num` as given, because Serply documents no maximum

    You.com and Sofya clamp because their APIs document a range and reject values
    outside it. Serply names no bound, so a clamp here would invent one; the
    returned list is still capped locally, which the next case pins.
    """
    seen: list[httpx.Request] = []
    await run_search({"results": []}, num_results=500, seen=seen)

    assert str(seen[0].url).endswith("&num=500")


async def test_returns_the_first_num_results_in_order(configured: None) -> None:
    """Stop at the caller's bound, keeping the API's ranking"""
    results = await run_search(results_payload(3), num_results=2)

    assert [result.title for result in results] == ["Result 0", "Result 1"]


async def test_keeps_a_result_that_has_no_description(configured: None) -> None:
    """Keep a usable link even with no snippet, since page_content is fetched later"""
    results = await run_search({"results": [{"title": "Result", "link": "https://example.org"}]})

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
        {"results": [{"title": "Result", "link": "https://example.org", "description": description}]}
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
                {"title": 7, "link": "https://odd-title.example/", "description": "s"},
                {"title": "Good", "link": "https://good.example/", "description": "ok"},
            ]
        }
    )

    assert [result.title for result in results] == ["Good"]


async def test_raises_when_no_returned_result_is_usable(configured: None) -> None:
    """Fail loudly instead of returning nothing when the schema does not match"""
    with pytest.raises(SerplyError, match=r"returned 3 result\(s\) but none could be parsed"):
        await run_search(
            {
                "results": [
                    {"headline": "no title or link"},
                    "not an object",
                    {"title": "Result", "link": 123},
                ]
            }
        )


async def test_returns_empty_when_the_api_found_nothing(configured: None) -> None:
    """Return no results, without error, when the query genuinely matched nothing"""
    assert await run_search({"results": [], "total": 0}) == []


async def test_raises_when_the_results_list_is_missing(configured: None) -> None:
    """A response without a `results` list is a schema change, not zero hits"""
    with pytest.raises(SerplyError, match="missing `results` list"):
        await run_search({"query": "q", "total": 0})


async def test_raises_when_results_is_not_a_list(configured: None) -> None:
    """Reject a `results` value that is not a list

    Driven with ``null``. With the guard removed, ``null`` is not iterable, so
    the provider raises ``TypeError`` and the case fails on the class. An object
    or a string would still be iterated, every element dropped by the item guard,
    and the none-parsed ``SerplyError`` raised instead -- a removal this case
    would then catch only through its message match.
    """
    with pytest.raises(SerplyError, match="missing `results` list"):
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
    """Report a missing SERPLY_API_KEY as configuration, before any request

    The exact class is asserted because ``SerplyConfigError`` subclasses
    ``SerplyError``, and a base-class check would also accept a parsing failure.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.delenv("SERPLY_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    with pytest.raises(SerplyConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is SerplyConfigError
    assert seen == []


async def test_a_whitespace_only_key_raises_config_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a key of only whitespace as missing rather than sending it

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("SERPLY_API_KEY", "   ")
    seen: list[httpx.Request] = []

    with pytest.raises(SerplyConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is SerplyConfigError
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

    await search_serply("q", num_results=1)

    assert seen[0].extensions["timeout"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
