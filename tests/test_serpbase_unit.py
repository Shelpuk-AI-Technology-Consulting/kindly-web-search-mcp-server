"""Unit tests for the SerpBase search provider.

SerpBase is second in the selection order and, until this module existed, was
executed by **no test in the tree** -- a whole provider's response parsing with
nothing asserting it. The layout mirrors the other provider modules: the request
shape and the parsing here, the transport-level failures (401, 429, a non-JSON
body, a wrong-shaped JSON body and a timeout) in
``test_search_provider_error_paths.py``, which drives all six providers from one
table.

**Written in pytest style, unlike its five sibling provider modules**, which are
``TestCase`` subclasses. Not a stylistic preference: ``scripts/check_plan_dag.py``
rejects a new ``unittest``-style module that no migration batch claims, and the
batch converting the provider tests to pytest has not run yet. Adding a sixth
``unittest`` module would mean enlarging that batch to convert a file written
after the decision to stop writing them. The guard is what surfaced this.

That guard reads the file as **text**, framework imports and class bases alike,
so spelling the framework name next to ``TestCase`` anywhere in this module --
this docstring included -- puts it back in the batch's scope. Measured.

SerpBase is also the only provider that sends its credential as a **query
parameter** rather than a header, so the request case asserts that rather than
copying a header assertion from a neighbour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search.serpbase import search_serpbase


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give SerpBase a dummy credential for the duration of one test.

    Args:
        monkeypatch: pytest's environment patcher, which restores the previous
            value when the test ends.
    """
    monkeypatch.setenv("SERPBASE_API_KEY", "serpbase_test")


async def run_search(
    payload: dict[str, Any],
    *,
    num_results: int = 5,
    query: str = "q",
    seen: list[httpx.Request] | None = None,
) -> list[WebSearchResult]:
    """Run ``search_serpbase`` against a mocked response.

    Args:
        payload: JSON body the mocked SerpBase API returns.
        num_results: Value forwarded to ``search_serpbase``.
        query: Query forwarded to ``search_serpbase``.
        seen: When given, receives each outgoing request.

    Returns:
        The parsed results.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await search_serpbase(query, num_results=num_results, http_client=client)


async def test_parses_organic_results(configured: None) -> None:
    """Parse the documented success shape into the server's result model"""
    results = await run_search(
        {
            "search_metadata": {"status": "Success"},
            "organic_results": [
                {
                    "title": "Apple",
                    "link": "https://www.apple.com/",
                    "snippet": "Discover the innovative world of Apple.",
                    "position": 1,
                }
            ],
        },
        num_results=1,
        query="apple inc",
    )

    assert len(results) == 1
    assert results[0].title == "Apple"
    assert results[0].link == "https://www.apple.com/"
    assert results[0].snippet == "Discover the innovative world of Apple."
    # `page_content` is filled in later by the MCP tool, never by the provider.
    assert results[0].page_content == ""


async def test_sends_the_documented_request(configured: None) -> None:
    """Send a GET whose credential travels as a parameter, not as a header

    Asserted separately from the parse because it is a separate claim, and
    because SerpBase is the only provider in the set that authenticates this way
    -- a header assertion copied from a neighbour would pass vacuously.
    """
    seen: list[httpx.Request] = []
    await run_search({"organic_results": []}, num_results=4, query="apple inc", seen=seen)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url.copy_with(query=None)) == "https://api.serpbase.dev/google/search"
    params = dict(request.url.params)
    assert params.get("q") == "apple inc"
    assert params.get("num") == "4"
    assert params.get("api_key") == "serpbase_test"


async def test_keeps_only_entries_carrying_three_strings(configured: None) -> None:
    """Drop what cannot be rendered rather than emitting a half-filled result"""
    results = await run_search(
        {
            "organic_results": [
                "not an object",
                {"title": "No link", "snippet": "s"},
                {"title": "Link is not a string", "link": 42, "snippet": "s"},
                {"link": "https://no-title.example/", "snippet": "s"},
                {
                    "title": "Snippet is not a string",
                    "link": "https://bad-snippet.example/",
                    "snippet": 7,
                },
                {"title": "Good", "link": "https://good.example/", "snippet": "ok"},
            ]
        }
    )

    assert [result.title for result in results] == ["Good"]


async def test_an_empty_organic_results_list_returns_no_results(configured: None) -> None:
    """Report a query with no hits as an empty list, not as an error"""
    assert await run_search({"organic_results": []}) == []


async def test_an_organic_results_value_that_is_not_a_list_returns_no_results(
    configured: None,
) -> None:
    """Survive a reshaped `organic_results`, as `serper.py` does for `organic`

    Driven with ``null`` rather than with an object, and the difference is
    measured: with the guard removed, an object is iterable, the loop walks its
    **keys** -- strings, which the item guard drops -- and the function still
    returns ``[]``. The case would pass on a provider that no longer checks the
    container at all. ``null`` is not iterable, so removing the guard raises
    ``TypeError`` and the case fails. It is also the shape a JSON API actually
    sends for "nothing here".

    Returning ``[]`` here, where ``sofya.py`` and ``youcom.py`` raise for the
    analogous shape, is an inconsistency between the six clients rather than a
    decision anyone recorded. This step pins the current behaviour and does not
    resolve it; section 14 of ``.system_design/TEST_SUITE.md`` names it.
    """
    assert await run_search({"organic_results": None}) == []


async def test_returns_at_most_num_results(configured: None) -> None:
    """Stop at the caller's bound even when the provider ignores `num`"""
    results = await run_search(
        {
            "organic_results": [
                {"title": f"R{i}", "link": f"https://e.example/{i}", "snippet": "s"}
                for i in range(5)
            ]
        },
        num_results=2,
    )

    assert [result.title for result in results] == ["R0", "R1"]
