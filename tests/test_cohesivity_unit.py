"""Unit tests for the Cohesivity search provider.

Cohesivity is ninth and last in the selection order. The layout mirrors
``test_serply_unit.py``: the request shape and the parsing live here, and the
transport-level failures (401, 429, a non-JSON body, a wrong-shaped JSON body and
a timeout) live in ``test_search_provider_error_paths.py``, which drives every
provider from one table. The tool-boundary disclosure sweep has a Cohesivity row
in ``test_provider_credential_disclosure.py``.

**Written in pytest style**, like ``test_serply_unit.py`` and for the reason
section 3.1 of ``.system_design/TEST_SUITE.md`` records.

**The application key travels in the URL**, as the ``key`` query parameter,
because the service accepts no other form. So beyond the request shape, this
module pins where the key is allowed to appear: on the wire in that one
parameter, and nowhere in a provider-authored message or a log record. The
router's conversion is what keeps it out of an HTTP failure, and two cases here
pin both halves of that -- that the raw ``httpx`` error *does* quote the key,
which is why the conversion is load-bearing, and that the converted one does not.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search import (
    PROVIDERS,
    SearchProviderTransportError,
    search_web,
)
from kindly_web_search_mcp_server.search.cohesivity import (
    SEARCH_ENDPOINT,
    SNIPPET_MAX_CHARS,
    CohesivityConfigError,
    CohesivityError,
    search_cohesivity,
)
from kindly_web_search_mcp_server.utils.logging import configure_logging

#: Captured before any case rebinds :class:`httpx.AsyncClient`, so the recording
#: double always subclasses the real client rather than an earlier double.
REAL_ASYNC_CLIENT = httpx.AsyncClient

#: Shaped like a real application key (``coh_app_`` and twenty lower-case
#: alphanumerics) but not one.
API_KEY = "coh_app_0000notarealkey0000"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Cohesivity a dummy credential for the duration of one test.

    Args:
        monkeypatch: pytest's environment patcher, which restores the previous
            value when the test ends.
    """
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", API_KEY)


@pytest.fixture
def only_cohesivity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Cohesivity the one configured provider, so the router selects it.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    for provider in PROVIDERS:
        monkeypatch.delenv(provider.env_var, raising=False)
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", API_KEY)


@pytest.fixture
def restored_logger_levels() -> Iterator[None]:
    """Restore the ``httpx`` and ``httpcore`` logger levels a case changes.

    Yields:
        Nothing; the levels are put back when the case ends.
    """
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels):
        logger.setLevel(level)


async def run_search(
    payload: Any,
    *,
    status: int = 200,
    num_results: int = 3,
    query: str = "q",
    seen: list[httpx.Request] | None = None,
) -> list[WebSearchResult]:
    """Run ``search_cohesivity`` against a mocked response.

    Args:
        payload: JSON body the mocked Cohesivity API returns.
        status: HTTP status the mocked API answers with.
        num_results: Value forwarded to ``search_cohesivity``.
        query: Query forwarded to ``search_cohesivity``.
        seen: When given, receives each outgoing request.

    Returns:
        The parsed results.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as client:
        return await search_cohesivity(
            query, num_results=num_results, http_client=client
        )


def results_payload(count: int) -> dict[str, Any]:
    """Build a response carrying ``count`` well-formed results.

    Args:
        count: How many results the payload holds.

    Returns:
        A Cohesivity-shaped response body whose results are titled ``Result 0``
        onward.
    """
    return {
        "requestId": "r",
        "results": [
            {
                "id": f"https://example.org/{i}",
                "title": f"Result {i}",
                "url": f"https://example.org/{i}",
                "highlights": ["s"],
            }
            for i in range(count)
        ],
    }


async def test_parses_documented_results(configured: None) -> None:
    """Parse the documented success shape into the server's result model"""
    results = await run_search(
        {
            "requestId": "b5947044c4b78efa9552a7c89b306d95",
            "resolvedSearchType": "neural",
            "results": [
                {
                    "id": "https://www.python-httpx.org/async/",
                    "title": "Async Support - HTTPX",
                    "url": "https://www.python-httpx.org/async/",
                    "publishedDate": "2024-01-01T00:00:00.000Z",
                    "author": None,
                    "highlights": ["HTTPX offers a standard synchronous API."],
                }
            ],
            "searchTime": 412.5,
            "costDollars": {"total": 0.005},
        },
        num_results=1,
    )

    assert results == [
        WebSearchResult(
            title="Async Support - HTTPX",
            link="https://www.python-httpx.org/async/",
            snippet="HTTPX offers a standard synchronous API.",
            # `page_content` is filled in later by the MCP tool, never here.
            page_content="",
        )
    ]


async def test_sends_the_documented_request(configured: None) -> None:
    """Send one POST with the key as the only query parameter and the body as documented

    The body is compared whole, so a dropped ``contents`` request -- which makes
    the service return results with no text to build a snippet from -- fails
    here as surely as a changed search type does.
    """
    seen: list[httpx.Request] = []
    await run_search({"results": []}, num_results=4, query="httpx", seen=seen)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url.copy_with(query=None)) == SEARCH_ENDPOINT
    assert request.url.scheme == "https"
    assert request.url.host == "cohesivity.ai"
    assert dict(request.url.params) == {"key": API_KEY}
    assert json.loads(request.content) == {
        "query": "httpx",
        "numResults": 4,
        "type": "auto",
        "contents": {"highlights": {"numSentences": 2}},
    }


async def test_the_key_travels_in_the_url_and_nowhere_else(configured: None) -> None:
    """Keep the key out of the headers and the body it does not authenticate

    The service rejects the key anywhere but the query string, so a copy in a
    header or the body is pure exposure: it would reach whatever logs those.
    """
    seen: list[httpx.Request] = []
    await run_search({"results": []}, query="httpx", seen=seen)

    request = seen[0]
    assert API_KEY in str(request.url)
    assert not any(API_KEY in value for value in request.headers.values())
    assert API_KEY not in request.content.decode()


async def test_forwards_a_large_num_results_unchanged(configured: None) -> None:
    """Send the caller's bound as given; the returned list is capped locally"""
    seen: list[httpx.Request] = []
    await run_search({"results": []}, num_results=500, seen=seen)

    assert json.loads(seen[0].content)["numResults"] == 500


async def test_returns_the_first_num_results_in_order(configured: None) -> None:
    """Stop at the caller's bound, keeping the API's ranking"""
    results = await run_search(results_payload(3), num_results=2)

    assert [result.title for result in results] == ["Result 0", "Result 1"]


async def test_joins_highlights_and_collapses_whitespace(configured: None) -> None:
    """Turn page-text highlights into one single-spaced snippet"""
    results = await run_search(
        {
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.org",
                    "highlights": ["  First\n\tsentence.  ", "Second   one."],
                }
            ]
        }
    )

    assert results[0].snippet == "First sentence. Second one."


async def test_caps_a_long_snippet(configured: None) -> None:
    """Bound the snippet, marking the cut, rather than passing page text through"""
    results = await run_search(
        {
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.org",
                    "highlights": ["word " * 400],
                }
            ]
        }
    )

    assert len(results[0].snippet) == SNIPPET_MAX_CHARS
    assert results[0].snippet.endswith("…")


async def test_keeps_a_snippet_of_exactly_the_cap(configured: None) -> None:
    """Leave a snippet that already fits untouched, at the boundary itself"""
    text = "x" * SNIPPET_MAX_CHARS
    results = await run_search(
        {
            "results": [
                {"title": "Result", "url": "https://example.org", "highlights": [text]}
            ]
        }
    )

    assert results[0].snippet == text


@pytest.mark.parametrize(
    "highlights",
    [None, "not a list", [], [7, None]],
    ids=["null", "string", "empty", "non-string-entries"],
)
async def test_keeps_a_result_without_usable_highlights(
    configured: None, highlights: object
) -> None:
    """Keep a usable link with an empty snippet, since page_content is fetched later

    A bare string is the value that separates a list check from iteration: a
    string iterates character by character and would otherwise become a
    snippet.

    Args:
        configured: Fixture providing the dummy credential.
        highlights: The malformed or missing value the API returns.
    """
    item: dict[str, Any] = {"title": "Result", "url": "https://example.org"}
    if highlights is not None:
        item["highlights"] = highlights

    results = await run_search({"results": [item]})

    assert [(result.title, result.snippet) for result in results] == [("Result", "")]


async def test_skips_non_string_highlight_entries(configured: None) -> None:
    """Use the string highlights and ignore the rest, rather than failing"""
    results = await run_search(
        {
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.org",
                    "highlights": [7, "kept", None],
                }
            ]
        }
    )

    assert results[0].snippet == "kept"


@pytest.mark.parametrize(
    "bad",
    [{"title": 7, "url": "https://odd.example/"}, {"title": "Odd", "url": None}],
    ids=["title", "url"],
)
async def test_skips_an_entry_without_a_string_title_and_url(
    configured: None, bad: dict[str, Any]
) -> None:
    """Drop a result missing either field, keeping its well-formed neighbour

    Each row breaks one field and keeps the other, so deleting either half of
    the check is observable.

    Args:
        configured: Fixture providing the dummy credential.
        bad: The entry with one unusable field.
    """
    results = await run_search(
        {"results": [bad, {"title": "Good", "url": "https://good.example/"}]}
    )

    assert [result.title for result in results] == ["Good"]


async def test_raises_when_no_returned_result_is_usable(configured: None) -> None:
    """Fail loudly instead of returning nothing when the schema does not match"""
    with pytest.raises(
        CohesivityError, match=r"returned 3 result\(s\) but none could be parsed"
    ):
        await run_search(
            {
                "results": [
                    {"id": "only an id"},
                    "not an object",
                    {"title": "Result", "link": "https://example.org"},
                ]
            }
        )


async def test_returns_empty_when_the_api_found_nothing(configured: None) -> None:
    """Return no results, without error, when the query genuinely matched nothing"""
    assert await run_search({"requestId": "r", "results": []}) == []


async def test_raises_when_the_results_list_is_missing(configured: None) -> None:
    """A response without a `results` list is a schema change, not zero hits"""
    with pytest.raises(CohesivityError, match="missing `results` list"):
        await run_search({"requestId": "r"})


async def test_raises_when_results_is_not_a_list(configured: None) -> None:
    """Reject a `results` value that is not a list

    Driven with ``null``: with the guard removed, ``null`` is not iterable and
    the provider raises ``TypeError``, which fails the class assertion.
    """
    with pytest.raises(CohesivityError, match="missing `results` list"):
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

    assert (
        await run_search(results_payload(1), num_results=num_results, seen=seen) == []
    )
    assert seen == []


@pytest.mark.parametrize(
    "value", [None, "", "   "], ids=["unset", "empty", "whitespace"]
)
async def test_an_unusable_key_raises_config_error_without_a_request(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Report a missing COHESIVITY_APPLICATION_KEY as configuration, before any request

    The exact class is asserted because ``CohesivityConfigError`` subclasses
    ``CohesivityError``, and a base-class check would also accept a parsing
    failure.

    Args:
        monkeypatch: pytest's environment patcher.
        value: The variable's value, or ``None`` to leave it unset.
    """
    if value is None:
        monkeypatch.delenv("COHESIVITY_APPLICATION_KEY", raising=False)
    else:
        monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", value)
    seen: list[httpx.Request] = []

    with pytest.raises(CohesivityConfigError) as raised:
        await run_search(results_payload(1), seen=seen)

    assert type(raised.value) is CohesivityConfigError
    assert "COHESIVITY_APPLICATION_KEY" in str(raised.value)
    assert seen == []


async def test_strips_whitespace_around_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send the key without the padding a pasted value often carries

    Args:
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", f"  {API_KEY}\n")
    seen: list[httpx.Request] = []

    await run_search({"results": []}, seen=seen)

    assert seen[0].url.params["key"] == API_KEY


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

    await search_cohesivity("q", num_results=1)

    assert seen[0].extensions["timeout"] == {
        "connect": 30,
        "read": 30,
        "write": 30,
        "pool": 30,
    }


# --------------------------------------------------------------------------
# Where the key must not appear.
# --------------------------------------------------------------------------


async def test_a_not_provisioned_403_reaches_the_caller_as_a_status_error(
    configured: None,
) -> None:
    """Let the service's 403 out as httpx raised it, and show why that is not the end

    A tenant that has not provisioned search answers 403. The provider does not
    intercept it -- the router owns HTTP failures -- so the caller of the
    coroutine sees ``httpx``'s own error, whose message quotes the request URL
    and with it the key. That is asserted, not avoided: it is the reason the
    router's conversion is load-bearing for this provider, and the next case
    pins that conversion.
    """
    body = {"error": {"code": 403, "message": "[Cohesivity] Service not provisioned"}}

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await run_search(body, status=403)

    assert type(raised.value) is httpx.HTTPStatusError
    assert raised.value.response.status_code == 403
    assert API_KEY in str(raised.value)


@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_the_router_reports_a_failure_without_the_key(
    only_cohesivity: None, status: int
) -> None:
    """Name the provider and the status, and nothing that came from the URL

    Covers the four statuses the service documents: a missing key, a rejected
    key, an unprovisioned tenant or a blocked search type, and a rate limit.

    Args:
        only_cohesivity: Fixture making Cohesivity the selected provider.
        status: The HTTP status the mocked service answers with.
    """
    body = {"error": {"code": status, "message": "[Cohesivity] denied"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))

    async with REAL_ASYNC_CLIENT(transport=transport) as client:
        with pytest.raises(SearchProviderTransportError) as raised:
            await search_web("q", num_results=1, http_client=client)

    assert str(raised.value) == f"The Cohesivity search provider failed: HTTP {status}."
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)


async def test_a_provider_error_message_carries_no_key(configured: None) -> None:
    """The provider's own messages are built from fixed text, never the request"""
    for payload, status in ((["not", "an", "object"], 200), ({"requestId": "r"}, 200)):
        with pytest.raises(CohesivityError) as raised:
            await run_search(payload, status=status)
        assert API_KEY not in str(raised.value)


async def test_no_log_record_carries_the_key_under_the_shipped_logging(
    configured: None,
    caplog: pytest.LogCaptureFixture,
    restored_logger_levels: None,
) -> None:
    """Under the server's own logging setup, a search logs nothing holding the key

    ``httpx`` logs every request URL at ``INFO``; ``configure_logging`` holds that
    logger at ``WARNING``, and that is all that keeps this provider's key out of
    the log stream. The root logger is opened to ``DEBUG`` so that the absence
    is the logger's doing rather than the capture's.

    Args:
        configured: Fixture providing the dummy credential.
        caplog: pytest's log capture.
        restored_logger_levels: Fixture undoing ``configure_logging``'s levels.
    """
    configure_logging()

    with caplog.at_level(logging.DEBUG):
        await run_search(results_payload(1))

    assert not [r for r in caplog.records if API_KEY in r.getMessage()]


async def test_httpx_request_logging_would_quote_the_key(
    configured: None,
    caplog: pytest.LogCaptureFixture,
    restored_logger_levels: None,
) -> None:
    """The control for the case above: the risk it guards against is real

    Opened to ``INFO``, the ``httpx`` logger quotes the full request URL, key
    included. A host that configures that logger itself would therefore log
    the key -- section 14 of ``.system_design/TEST_SUITE.md`` records it. Without
    this case, the one above would pass just as well against a client that never
    logged anything.

    Args:
        configured: Fixture providing the dummy credential.
        caplog: pytest's log capture.
        restored_logger_levels: Fixture restoring the ``httpx`` logger level.
    """
    with caplog.at_level(logging.INFO, logger="httpx"):
        await run_search(results_payload(1))

    assert [r for r in caplog.records if API_KEY in r.getMessage()]
