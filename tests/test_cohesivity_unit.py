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

**Zero-setup mode** (``COHESIVITY_APPLICATION_KEY=auto``) is the second half of
the module. It is driven against :class:`FakeCohesivity`, one mocked transport
answering both the search endpoint and the hosted MCP endpoint, with ``HOME``,
``XDG_CONFIG_HOME``, ``APPDATA`` and the working directory all moved under
``tmp_path`` so no case can see or write a real credentials file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search import (
    PROVIDERS,
    SearchProviderTransportError,
    cohesivity,
    search_web,
)
from kindly_web_search_mcp_server.search.cohesivity import (
    MCP_ENDPOINT,
    SEARCH_ENDPOINT,
    SEARCH_RESOURCE,
    SNIPPET_MAX_CHARS,
    STATE_FIELDS,
    CohesivityAllowanceError,
    CohesivityBootstrapError,
    CohesivityConfigError,
    CohesivityError,
    CohesivityStateError,
    search_cohesivity,
    state_file_path,
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


# --------------------------------------------------------------------------
# Zero-setup mode: COHESIVITY_APPLICATION_KEY=auto.
# --------------------------------------------------------------------------

#: Fake keys, shaped like the real ones but not real. Lower-case, so
#: ``test_baseline_failure_ledger`` does not read them as variable names.
PROJECT_APP_KEY = "coh_app_project0notarealkey"
PROJECT_MGMT_KEY = "coh_mgmt_project0notarealkey"
SAVED_APP_KEY = "coh_app_saved0notarealkey00"
SAVED_MGMT_KEY = "coh_mgmt_saved0notarealkey0"

FAR_FUTURE = "2099-01-01T00:00:00.000Z"
LONG_AGO = "2001-01-01T00:00:00.000Z"
CLAIM_URL = "https://cohesivity.ai/c/claimtoken0000"

#: The edge's own error bodies, shaped as ``worker/src/helpers.js`` and
#: ``usage.js`` build them.
NOT_PROVISIONED = {
    "error": {
        "code": 403,
        "message": "[Cohesivity] Service not provisioned: You must provision "
        f'"{SEARCH_RESOURCE}" via POST /api/resources/{SEARCH_RESOURCE} before '
        "using the edge",
    }
}
PAUSED = {
    "error": {
        "code": 403,
        "message": "[Cohesivity] Tenant paused: Claim this tenant to resume service.",
    },
    "tenant_state": "paused",
    "pause_reason": {"kind": "all_time_cap", "recommended_action": "claim_to_lift"},
    "tenant_lifecycle": "ephemeral",
}
LIFETIME_429 = {
    "error": {
        "code": 429,
        "message": f"[Cohesivity] Plan limit exceeded for {SEARCH_RESOURCE}",
    },
    "tenant_state": "plan_limit_exceeded",
    "window_kind": "all_time",
}
PER_MINUTE_429 = {
    "error": {
        "code": 429,
        "message": f"[Cohesivity] Rate limit exceeded for {SEARCH_RESOURCE}",
    },
    "tenant_state": "active",
    "window_kind": "utc_minute",
}
INVALID_KEY = {
    "error": {"code": 401, "message": "[Cohesivity] Invalid application key"}
}
EXPIRED = {
    "error": {"code": 410, "message": "[Cohesivity] Tenant expired"},
    "tenant_state": "expired",
}


def created_keys(n: int) -> tuple[str, str]:
    """Return the fake application and management keys of the ``n``-th tenant.

    Args:
        n: The tenant's creation number, from 1.

    Returns:
        The application key and the management key.
    """
    return f"coh_app_created{n}notarealkey", f"coh_mgmt_created{n}notarealkey"


def mcp_reply(structured: dict[str, Any], *, sse: bool = False) -> httpx.Response:
    """Build a successful ``tools/call`` reply.

    Args:
        structured: The tool's ``structuredContent``.
        sse: Send it as a server-sent-events body instead of JSON.

    Returns:
        The reply.
    """
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(structured)}],
            "structuredContent": structured,
            "isError": False,
        },
    }
    if sse:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"event: message\ndata: {json.dumps(message)}\n\n",
        )
    return httpx.Response(200, json=message)


def mcp_tool_error(text: str) -> httpx.Response:
    """Build a ``tools/call`` reply reporting a tool error.

    Args:
        text: The error text the tool returns.

    Returns:
        The reply.
    """
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        },
    )


def mcp_rpc_error(message: str) -> httpx.Response:
    """Build a JSON-RPC error reply.

    Args:
        message: The error's message.

    Returns:
        The reply.
    """
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": message}},
    )


class FakeCohesivity:
    """A mocked Cohesivity: the search endpoint and the hosted MCP endpoint.

    Attributes:
        calls: Each request as ``(name, detail)``: ``("search", key)`` for a
            search, ``(tool, arguments)`` for an MCP call.
        requests: Each raw request, in order.
        tools: The handler for each MCP tool, replaceable by a case.
        edge: The search handler, called with the key the request carried.
        created: How many tenants ``create_tenant`` has created.
        sse: Answer MCP calls with server-sent-events bodies.
    """

    def __init__(self) -> None:
        """Start with every tool succeeding and every search returning a result."""
        self.calls: list[tuple[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.replies: list[bytes] = []
        self.created = 0
        self.sse = False
        self.create_delay = 0.0
        self.tools: dict[str, Callable[[dict[str, Any]], httpx.Response]] = {
            "create_tenant": self.create_tenant,
            "provision_resource": lambda args: mcp_reply(
                {
                    "resource": SEARCH_RESOURCE,
                    "result": {"success": True, "status": "active"},
                },
                sse=self.sse,
            ),
            "tenant_status": lambda args: mcp_reply(
                {"status": {"account": {"status": "active", "lifecycle": "ephemeral"}}},
                sse=self.sse,
            ),
            "claim_tenant": lambda args: mcp_reply(
                {"tenant_id": args["tenant_id"], "approval_url": CLAIM_URL},
                sse=self.sse,
            ),
        }
        self.edge: Callable[[str], httpx.Response] = lambda key: httpx.Response(
            200, json=results_payload(1)
        )

    def create_tenant(self, arguments: dict[str, Any]) -> httpx.Response:
        """Create the next fake tenant.

        Args:
            arguments: The tool arguments.

        Returns:
            The reply, carrying the new keys in ``credentials_file``.
        """
        self.created += 1
        app_key, mgmt_key = created_keys(self.created)
        content = (
            "# Cohesivity credentials -- keep out of version control\n"
            f"tenant_id=tenant_{self.created}\n"
            f"coh_management_key={mgmt_key}\n"
            f"coh_application_key={app_key}\n"
            f"expires_at={FAR_FUTURE}\n"
        )
        return mcp_reply(
            {
                "tenant_id": f"tenant_{self.created}",
                "expires_at": FAR_FUTURE,
                "runtime_profile": "default",
                "tenant_lifecycle": "ephemeral",
                "credentials_file": {"filename": ".cohesivity", "content": content},
            },
            sse=self.sse,
        )

    def count(self, name: str) -> int:
        """Count the calls of one kind.

        Args:
            name: ``"search"`` or an MCP tool name.

        Returns:
            How many there were.
        """
        return sum(1 for call, _detail in self.calls if call == name)

    def mcp_calls(self) -> list[str]:
        """List the MCP tools called, in order.

        Returns:
            The tool names.
        """
        return [call for call, _detail in self.calls if call != "search"]

    def searched_with(self) -> list[str]:
        """List the keys each search carried, in order.

        Returns:
            The keys.
        """
        return [detail for call, detail in self.calls if call == "search"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request, recording it and the reply.

        Args:
            request: The outgoing request.

        Returns:
            The scripted response.
        """
        response = await self._answer(request)
        self.replies.append(response.content)
        return response

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        """Route one request to the scripted search or tool handler.

        Args:
            request: The outgoing request.

        Returns:
            The scripted response.
        """
        self.requests.append(request)
        if str(request.url) == MCP_ENDPOINT:
            body = json.loads(request.content)
            assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
            name = body["params"]["name"]
            arguments = body["params"]["arguments"]
            self.calls.append((name, arguments))
            if name == "create_tenant" and self.create_delay:
                # A real suspension point, so concurrent searches interleave.
                await asyncio.sleep(self.create_delay)
            return self.tools[name](arguments)
        assert str(request.url.copy_with(query=None)) == SEARCH_ENDPOINT
        key = request.url.params["key"]
        self.calls.append(("search", key))
        await asyncio.sleep(0)
        return self.edge(key)


@pytest.fixture(autouse=True)
def fresh_bootstrap_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each case its own lock, since each runs in its own event loop.

    Args:
        monkeypatch: pytest's patcher, which restores the module's lock.
    """
    monkeypatch.setattr(cohesivity, "_BOOTSTRAP_LOCK", asyncio.Lock())


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every location zero-setup mode reads, and select it.

    ``HOME`` is ``tmp_path/home``, the config directory is under it, and the
    working directory is ``home/work/project``, so the project-file walk stops
    at ``home`` and never reaches a real ``.cohesivity``.

    Args:
        tmp_path: pytest's per-case directory.
        monkeypatch: pytest's patcher.

    Returns:
        The isolated home directory.
    """
    home = tmp_path / "home"
    project = home / "work" / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.chdir(project)
    for provider in PROVIDERS:
        monkeypatch.delenv(provider.env_var, raising=False)
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", "auto")
    return home


def write_project_file(directory: Path, *, management_key: bool = True) -> Path:
    """Write a project ``.cohesivity`` file, as Cohesivity's tooling does.

    Args:
        directory: Where to write it.
        management_key: Include the management key.

    Returns:
        The file's path.
    """
    lines = [
        "# Cohesivity project credentials",
        "tenant_id=tenant_project",
        f"coh_application_key={PROJECT_APP_KEY}",
        f"expires_at={LONG_AGO}",
    ]
    if management_key:
        lines.insert(2, f"coh_management_key={PROJECT_MGMT_KEY}")
    path = directory / ".cohesivity"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_state(*, expires_at: str = FAR_FUTURE) -> Path:
    """Write this server's state file for a saved tenant.

    Args:
        expires_at: The saved tenant's expiry.

    Returns:
        The file's path.
    """
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tenant_id": "tenant_saved",
                "coh_management_key": SAVED_MGMT_KEY,
                "coh_application_key": SAVED_APP_KEY,
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )
    return path


def read_state() -> dict[str, Any]:
    """Read the state file back.

    Returns:
        Its JSON object.
    """
    return json.loads(state_file_path().read_text(encoding="utf-8"))


async def run_auto(
    fake: FakeCohesivity, *, num_results: int = 1
) -> list[WebSearchResult]:
    """Run ``search_cohesivity`` against the fake service.

    Args:
        fake: The fake service.
        num_results: Value forwarded to ``search_cohesivity``.

    Returns:
        The parsed results.
    """
    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        return await search_cohesivity("q", num_results=num_results, http_client=client)


def every_key() -> tuple[str, ...]:
    """List every fake key a case may have put in flight.

    Returns:
        The keys.
    """
    created = [key for n in range(1, 4) for key in created_keys(n)]
    return (PROJECT_APP_KEY, PROJECT_MGMT_KEY, SAVED_APP_KEY, SAVED_MGMT_KEY, *created)


async def test_an_explicit_key_never_bootstraps_or_touches_files(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real key is sent as given, with project and state files ignored

    Both files are present and the state directory is watched, so reading
    either -- or creating anything -- would show.

    Args:
        home: Fixture isolating zero-setup mode's locations.
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", API_KEY)
    project_file = write_project_file(Path.cwd())
    state = write_state()
    before = (project_file.read_bytes(), state.read_bytes())
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(403, json=NOT_PROVISIONED)

    with pytest.raises(httpx.HTTPStatusError):
        await run_auto(fake)

    assert fake.calls == [("search", API_KEY)]
    assert (project_file.read_bytes(), state.read_bytes()) == before


async def test_an_explicit_key_creates_no_state_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written for a real key, not even an empty directory

    Args:
        home: Fixture isolating zero-setup mode's locations.
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", API_KEY)
    fake = FakeCohesivity()

    await run_auto(fake)

    assert fake.calls == [("search", API_KEY)]
    assert not state_file_path().parent.exists()


@pytest.mark.parametrize("value", ["auto", "AUTO", "Auto", "  aUtO \n"])
async def test_auto_is_case_insensitive_and_trimmed(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Every spelling of ``auto`` selects zero-setup mode, not a key named "AUTO"

    Args:
        home: Fixture isolating zero-setup mode's locations.
        monkeypatch: pytest's environment patcher.
        value: The variable's value.
    """
    monkeypatch.setenv("COHESIVITY_APPLICATION_KEY", value)
    fake = FakeCohesivity()

    await run_auto(fake)

    assert fake.created == 1
    assert fake.searched_with() == [created_keys(1)[0]]


async def test_a_project_file_in_a_parent_directory_is_used_read_only(
    home: Path,
) -> None:
    """Search with a parent directory's ``.cohesivity``, leaving it untouched

    Its ``expires_at`` is in the past on purpose: a project file's tenant is
    never checked or replaced, so even that costs no MCP call.

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    project_file = write_project_file(Path.cwd().parent)
    before = project_file.read_bytes()
    mtime = project_file.stat().st_mtime_ns
    fake = FakeCohesivity()

    results = await run_auto(fake)

    assert [result.title for result in results] == ["Result 0"]
    assert fake.calls == [("search", PROJECT_APP_KEY)]
    assert project_file.read_bytes() == before
    assert project_file.stat().st_mtime_ns == mtime
    assert not state_file_path().exists()


async def test_the_project_file_walk_stops_at_the_home_directory(
    home: Path,
) -> None:
    """A ``.cohesivity`` above the home directory is not someone's project

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_project_file(home.parent)
    fake = FakeCohesivity()

    await run_auto(fake)

    assert fake.searched_with() == [created_keys(1)[0]]


async def test_a_project_file_without_an_application_key_is_passed_over(
    home: Path,
) -> None:
    """A file with no usable key does not stop the search for one

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    (Path.cwd() / ".cohesivity").write_text("# empty\ntenant_id=x\n", encoding="utf-8")
    write_project_file(Path.cwd().parent)
    fake = FakeCohesivity()

    await run_auto(fake)

    assert fake.calls == [("search", PROJECT_APP_KEY)]


async def test_saved_state_is_reused_with_no_mcp_call(home: Path) -> None:
    """A saved tenant is searched with directly

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()

    await run_auto(fake)
    await run_auto(fake)

    assert fake.calls == [("search", SAVED_APP_KEY), ("search", SAVED_APP_KEY)]


async def test_concurrent_searches_bootstrap_exactly_one_tenant(home: Path) -> None:
    """Five searches at once create one tenant, provision it once, and share it

    ``create_tenant`` suspends, so the five genuinely overlap: without the lock,
    or without re-reading the state file under it, each would create its own.

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()
    fake.create_delay = 0.05

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        outcomes = await asyncio.gather(
            *(
                search_cohesivity("q", num_results=1, http_client=client)
                for _ in range(5)
            )
        )

    app_key, mgmt_key = created_keys(1)
    assert all(len(results) == 1 for results in outcomes)
    assert fake.count("create_tenant") == 1
    assert fake.count("provision_resource") == 1
    assert fake.mcp_calls()[:2] == ["create_tenant", "provision_resource"]
    assert fake.searched_with() == [app_key] * 5
    provision = next(args for call, args in fake.calls if call == "provision_resource")
    assert provision == {
        "tenant_id": "tenant_1",
        "resource": SEARCH_RESOURCE,
        "confirmed": True,
        "coh_management_key": mgmt_key,
    }

    path = state_file_path()
    assert read_state() == {
        "tenant_id": "tenant_1",
        "coh_management_key": mgmt_key,
        "coh_application_key": app_key,
        "expires_at": FAR_FUTURE,
    }
    assert tuple(read_state()) == STATE_FIELDS
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


async def test_the_create_call_is_a_bare_json_rpc_post_with_explicit_headers(
    home: Path,
) -> None:
    """Send ``tools/call`` with the Accept and User-Agent the endpoint needs

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()

    await run_auto(fake)

    create = fake.requests[0]
    assert create.method == "POST"
    assert str(create.url) == MCP_ENDPOINT
    assert create.headers["accept"] == "application/json, text/event-stream"
    assert create.headers["user-agent"] == "kindly-web-search-mcp-server"
    assert create.headers["content-type"] == "application/json"
    assert create.extensions["timeout"]["read"] == 30
    assert json.loads(create.content)["params"] == {
        "name": "create_tenant",
        "arguments": {"confirmed": True},
    }


def test_the_state_file_honours_xdg_config_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``XDG_CONFIG_HOME`` decides the directory; a relative one is ignored

    Args:
        home: Fixture isolating zero-setup mode's locations.
        tmp_path: pytest's per-case directory.
        monkeypatch: pytest's environment patcher.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert (
        state_file_path()
        == tmp_path / "xdg" / "kindly-web-search" / "cohesivity-tenant.json"
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert (
        state_file_path()
        == home / ".config" / "kindly-web-search" / "cohesivity-tenant.json"
    )

    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert (
        state_file_path()
        == home / ".config" / "kindly-web-search" / "cohesivity-tenant.json"
    )


def test_the_state_file_lives_under_appdata_on_windows(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``%APPDATA%`` decides the directory on Windows

    Args:
        home: Fixture isolating zero-setup mode's locations.
        monkeypatch: pytest's patcher, here of ``os.name`` for this one call.
    """
    with monkeypatch.context() as patched:
        patched.setattr(cohesivity, "_WINDOWS", True)
        path = state_file_path()

    assert path == home / "AppData" / "kindly-web-search" / "cohesivity-tenant.json"


@pytest.mark.parametrize("target", ["state", "directory"])
async def test_a_symlinked_state_location_is_refused_before_any_call(
    home: Path, tmp_path: Path, target: str
) -> None:
    """Neither read through nor write through a symlink, and create nothing

    Args:
        home: Fixture isolating zero-setup mode's locations.
        tmp_path: pytest's per-case directory.
        target: Whether the file or its directory is the link.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = state_file_path()
    if target == "state":
        path.parent.mkdir(parents=True)
        (elsewhere / "victim.json").write_text("{}", encoding="utf-8")
        path.symlink_to(elsewhere / "victim.json")
    else:
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(elsewhere, target_is_directory=True)
    fake = FakeCohesivity()

    with pytest.raises(CohesivityStateError, match="symbolic link"):
        await run_auto(fake)

    assert fake.calls == []
    assert sorted(p.name for p in elsewhere.iterdir()) == (
        ["victim.json"] if target == "state" else []
    )


def _raise_timeout(arguments: dict[str, Any]) -> httpx.Response:
    """Fail an MCP call as a read timeout.

    Args:
        arguments: The tool arguments, unused.

    Raises:
        httpx.ReadTimeout: Always.
    """
    raise httpx.ReadTimeout("timed out")


def _raise_connect_error(arguments: dict[str, Any]) -> httpx.Response:
    """Fail an MCP call as a network error.

    Args:
        arguments: The tool arguments, unused.

    Raises:
        httpx.ConnectError: Always.
    """
    raise httpx.ConnectError("connection reset")


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (_raise_timeout, "did not complete (ReadTimeout)"),
        (_raise_connect_error, "did not complete (ConnectError)"),
        (lambda args: httpx.Response(502, text="bad gateway"), "failed with HTTP 502"),
        (
            lambda args: httpx.Response(200, text="<html>nope</html>"),
            "unreadable reply",
        ),
        (lambda args: mcp_reply({"tenant_id": "t"}), "returned no usable credentials"),
    ],
    ids=["timeout", "network", "5xx", "unparseable", "no-credentials"],
)
async def test_an_ambiguous_create_is_never_retried(
    home: Path, reply: Callable[[dict[str, Any]], httpx.Response], detail: str
) -> None:
    """A create whose outcome is unknown fails once, clearly, with nothing saved

    Retrying could leave a second tenant behind the first, so the call is made
    exactly once and the error says it was not retried.

    Args:
        home: Fixture isolating zero-setup mode's locations.
        reply: The create call's scripted outcome.
        detail: The text the error must carry.
    """
    fake = FakeCohesivity()
    fake.tools["create_tenant"] = reply

    with pytest.raises(CohesivityBootstrapError) as raised:
        await run_auto(fake)

    assert raised.value.ambiguous is True
    assert detail in str(raised.value)
    assert "not retried" in str(raised.value)
    assert fake.mcp_calls() == ["create_tenant"]
    assert fake.searched_with() == []
    assert not state_file_path().exists()


async def test_searches_waiting_on_a_failed_create_do_not_create_again(
    home: Path,
) -> None:
    """Concurrent searches share one create call even when it times out

    Each search queued behind the lock would otherwise find no state file and
    call ``create_tenant`` itself -- an automatic retry of a creation whose
    outcome is unknown, possibly leaving several tenants behind.

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()
    fake.create_delay = 0.05
    fake.tools["create_tenant"] = _raise_timeout

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        outcomes = await asyncio.gather(
            *(
                search_cohesivity("q", num_results=1, http_client=client)
                for _ in range(4)
            ),
            return_exceptions=True,
        )

    assert all(isinstance(outcome, CohesivityBootstrapError) for outcome in outcomes)
    assert all(outcome.ambiguous for outcome in outcomes)
    assert fake.mcp_calls() == ["create_tenant"]
    assert fake.searched_with() == []
    assert not state_file_path().exists()


async def test_a_later_search_after_a_failed_create_may_try_again(home: Path) -> None:
    """The refusal covers only searches that waited on the failure, not later ones

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()
    fake.tools["create_tenant"] = _raise_timeout
    with pytest.raises(CohesivityBootstrapError):
        await run_auto(fake)

    fake.tools["create_tenant"] = fake.create_tenant
    results = await run_auto(fake)

    assert len(results) == 1
    assert fake.mcp_calls() == ["create_tenant", "create_tenant", "provision_resource"]


async def test_searches_waiting_on_a_failed_replacement_do_not_create_again(
    home: Path,
) -> None:
    """A rejected saved tenant whose replacement fails is not replaced again

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.create_delay = 0.05
    fake.tools["create_tenant"] = lambda args: httpx.Response(502)
    fake.edge = lambda key: httpx.Response(401, json=INVALID_KEY)

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        outcomes = await asyncio.gather(
            *(
                search_cohesivity("q", num_results=1, http_client=client)
                for _ in range(3)
            ),
            return_exceptions=True,
        )

    assert all(isinstance(outcome, CohesivityBootstrapError) for outcome in outcomes)
    assert fake.count("create_tenant") == 1


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (lambda args: mcp_tool_error("create failed"), "reported an error"),
        (
            lambda args: mcp_rpc_error("Invalid params"),
            "was refused (JSON-RPC error -32602)",
        ),
        (
            lambda args: mcp_tool_error(
                "[Cohesivity] Too many tenant creation requests from this IP. Retry in 42s."
            ),
            "was rate-limited",
        ),
    ],
    ids=["is-error", "json-rpc-error", "rate-limited"],
)
async def test_a_refused_create_fails_once_without_quoting_the_reply(
    home: Path, reply: Callable[[dict[str, Any]], httpx.Response], detail: str
) -> None:
    """``isError`` and a JSON-RPC ``error`` are definite failures, also not retried

    Args:
        home: Fixture isolating zero-setup mode's locations.
        reply: The create call's scripted reply.
        detail: The fixed text the error must carry.
    """
    fake = FakeCohesivity()
    fake.tools["create_tenant"] = reply

    with pytest.raises(CohesivityBootstrapError) as raised:
        await run_auto(fake)

    assert raised.value.ambiguous is False
    assert detail in str(raised.value)
    for remote_text in ("create failed", "Invalid params", "Retry in 42s"):
        assert remote_text not in str(raised.value)
    assert fake.mcp_calls() == ["create_tenant"]


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_mcp_replies_are_read_as_json_or_as_server_sent_events(
    home: Path, sse: bool
) -> None:
    """Bootstrap the same way whichever body form the endpoint chooses

    Args:
        home: Fixture isolating zero-setup mode's locations.
        sse: Whether the fake answers with server-sent events.
    """
    fake = FakeCohesivity()
    fake.sse = sse

    await run_auto(fake)

    assert fake.mcp_calls() == ["create_tenant", "provision_resource"]
    assert read_state()["coh_application_key"] == created_keys(1)[0]


def test_an_sse_body_yields_its_last_json_message() -> None:
    """Skip comments, keep-alives and non-JSON data; join multi-line data"""
    body = (
        ": keep-alive\n\n"
        "event: message\ndata: not json\n\n"
        'event: message\ndata: {"jsonrpc": "2.0",\ndata:  "id": 1, "result": {}}\n\n'
    )

    assert cohesivity._last_sse_message(body) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }


async def test_a_failed_provision_after_create_is_repaired_without_a_second_tenant(
    home: Path,
) -> None:
    """The tenant is saved before provisioning, so the next search provisions it

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()
    fake.tools["provision_resource"] = lambda args: httpx.Response(503)

    with pytest.raises(CohesivityBootstrapError, match="provision_resource"):
        await run_auto(fake)
    assert read_state()["tenant_id"] == "tenant_1"

    provisioned = False

    def provision(args: dict[str, Any]) -> httpx.Response:
        """Succeed, and record that search is now provisioned.

        Args:
            args: The tool arguments, unused.

        Returns:
            A successful reply.
        """
        nonlocal provisioned
        provisioned = True
        return mcp_reply({"resource": SEARCH_RESOURCE, "result": {"success": True}})

    fake.tools["provision_resource"] = provision
    fake.edge = lambda key: (
        httpx.Response(200, json=results_payload(1))
        if provisioned
        else httpx.Response(403, json=NOT_PROVISIONED)
    )
    fake.calls.clear()

    await run_auto(fake)

    assert fake.created == 1
    assert fake.calls[0] == ("search", created_keys(1)[0])
    assert [call for call, _ in fake.calls] == [
        "search",
        "provision_resource",
        "search",
    ]


@pytest.mark.parametrize("source", ["state", "project"])
async def test_not_provisioned_provisions_once_and_retries_once(
    home: Path, source: str
) -> None:
    """A 403 "Service not provisioned" costs one provision call and one retry

    Args:
        home: Fixture isolating zero-setup mode's locations.
        source: Where the tenant's credentials come from.
    """
    if source == "state":
        write_state()
        app_key, mgmt_key = SAVED_APP_KEY, SAVED_MGMT_KEY
    else:
        write_project_file(Path.cwd())
        app_key, mgmt_key = PROJECT_APP_KEY, PROJECT_MGMT_KEY
    fake = FakeCohesivity()
    answers = iter([httpx.Response(403, json=NOT_PROVISIONED)])
    fake.edge = lambda key: next(answers, httpx.Response(200, json=results_payload(1)))

    results = await run_auto(fake)

    assert len(results) == 1
    assert [call for call, _ in fake.calls] == [
        "search",
        "provision_resource",
        "search",
    ]
    assert fake.searched_with() == [app_key, app_key]
    assert fake.calls[1][1]["coh_management_key"] == mgmt_key


async def test_not_provisioned_twice_is_left_to_the_router(home: Path) -> None:
    """A tenant still unprovisioned after one repair fails as a plain 403

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(403, json=NOT_PROVISIONED)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await run_auto(fake)

    assert raised.value.response.status_code == 403
    assert [call for call, _ in fake.calls] == [
        "search",
        "provision_resource",
        "search",
    ]


async def test_not_provisioned_without_a_management_key_is_not_repaired(
    home: Path,
) -> None:
    """A project file with no management key cannot provision; nothing is created

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_project_file(Path.cwd(), management_key=False)
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(403, json=NOT_PROVISIONED)

    with pytest.raises(httpx.HTTPStatusError):
        await run_auto(fake)

    assert fake.calls == [("search", PROJECT_APP_KEY)]


async def test_an_expired_saved_tenant_is_replaced_before_searching(home: Path) -> None:
    """Past ``expires_at`` and unclaimed: discard it, create one, search with that

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state(expires_at=LONG_AGO)
    fake = FakeCohesivity()

    await run_auto(fake)

    assert fake.mcp_calls() == ["tenant_status", "create_tenant", "provision_resource"]
    assert fake.calls[0][1] == {
        "tenant_id": "tenant_saved",
        "coh_management_key": SAVED_MGMT_KEY,
    }
    assert fake.searched_with() == [created_keys(1)[0]]
    assert read_state()["tenant_id"] == "tenant_1"


async def test_an_expired_but_claimed_saved_tenant_is_kept(home: Path) -> None:
    """A claimed tenant outlives its anonymous expiry; keep it and save the new one

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state(expires_at=LONG_AGO)
    fake = FakeCohesivity()
    fake.tools["tenant_status"] = lambda args: mcp_reply(
        {
            "status": {
                "account": {
                    "status": "active",
                    "lifecycle": "claimed",
                    "expires_at": None,
                }
            }
        }
    )

    await run_auto(fake)
    await run_auto(fake)

    assert fake.mcp_calls() == ["tenant_status"]
    assert fake.searched_with() == [SAVED_APP_KEY, SAVED_APP_KEY]
    assert read_state()["expires_at"] is None


async def test_an_unreachable_status_check_leaves_the_decision_to_the_edge(
    home: Path,
) -> None:
    """If ``tenant_status`` cannot answer, search with the saved tenant anyway

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state(expires_at=LONG_AGO)
    fake = FakeCohesivity()
    fake.tools["tenant_status"] = _raise_timeout

    await run_auto(fake)

    assert fake.mcp_calls() == ["tenant_status"]
    assert fake.searched_with() == [SAVED_APP_KEY]


@pytest.mark.parametrize(
    ("status", "body"), [(401, INVALID_KEY), (410, EXPIRED)], ids=["401", "410"]
)
async def test_a_rejected_saved_tenant_is_replaced_once_and_retried_once(
    home: Path, status: int, body: dict[str, Any]
) -> None:
    """The edge's 401 or 410 for the saved tenant: one new tenant, one retry

    Args:
        home: Fixture isolating zero-setup mode's locations.
        status: The edge's status for the saved tenant.
        body: The edge's error body.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: (
        httpx.Response(status, json=body)
        if key == SAVED_APP_KEY
        else httpx.Response(200, json=results_payload(1))
    )

    results = await run_auto(fake)

    assert len(results) == 1
    assert fake.searched_with() == [SAVED_APP_KEY, created_keys(1)[0]]
    assert fake.created == 1
    assert read_state()["tenant_id"] == "tenant_1"


async def test_a_replacement_tenant_is_created_only_once_per_search(
    home: Path,
) -> None:
    """If the new tenant is rejected too, fail rather than create another

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(401, json=INVALID_KEY)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await run_auto(fake)

    assert raised.value.response.status_code == 401
    assert fake.created == 1
    assert fake.searched_with() == [SAVED_APP_KEY, created_keys(1)[0]]


async def test_a_freshly_bootstrapped_tenant_is_not_replaced(home: Path) -> None:
    """A tenant this search just created is not swapped for yet another one

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(410, json=EXPIRED)

    with pytest.raises(httpx.HTTPStatusError):
        await run_auto(fake)

    assert fake.created == 1


async def test_concurrent_rejections_of_the_saved_tenant_replace_it_once(
    home: Path,
) -> None:
    """Searches that all saw the old tenant rejected share one replacement

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.create_delay = 0.05
    fake.edge = lambda key: (
        httpx.Response(401, json=INVALID_KEY)
        if key == SAVED_APP_KEY
        else httpx.Response(200, json=results_payload(1))
    )

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        await asyncio.gather(
            *(
                search_cohesivity("q", num_results=1, http_client=client)
                for _ in range(4)
            )
        )

    assert fake.created == 1


@pytest.mark.parametrize(
    ("status", "body"), [(401, INVALID_KEY), (410, EXPIRED)], ids=["401", "410"]
)
async def test_a_project_file_tenant_is_never_replaced(
    home: Path, status: int, body: dict[str, Any]
) -> None:
    """A rejected project tenant fails as a plain status error; nothing is created

    Args:
        home: Fixture isolating zero-setup mode's locations.
        status: The edge's status.
        body: The edge's error body.
    """
    project_file = write_project_file(Path.cwd())
    before = project_file.read_bytes()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(status, json=body)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await run_auto(fake)

    assert raised.value.response.status_code == status
    assert fake.calls == [("search", PROJECT_APP_KEY)]
    assert project_file.read_bytes() == before
    assert not state_file_path().exists()


@pytest.mark.parametrize("source", ["state", "project"])
@pytest.mark.parametrize(
    ("status", "body"),
    [(403, PAUSED), (429, LIFETIME_429)],
    ids=["paused", "lifetime-quota"],
)
async def test_a_used_up_allowance_returns_the_claim_link_and_no_new_tenant(
    home: Path, source: str, status: int, body: dict[str, Any]
) -> None:
    """Paused or out of lifetime quota: ask for the claim link, create nothing

    Args:
        home: Fixture isolating zero-setup mode's locations.
        source: Where the tenant's credentials come from.
        status: The edge's status.
        body: The edge's error body.
    """
    if source == "state":
        write_state()
        tenant_id, mgmt_key = "tenant_saved", SAVED_MGMT_KEY
    else:
        write_project_file(Path.cwd())
        tenant_id, mgmt_key = "tenant_project", PROJECT_MGMT_KEY
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(status, json=body)

    with pytest.raises(CohesivityAllowanceError) as raised:
        await run_auto(fake)

    assert str(raised.value) == (
        "Cohesivity search: the free anonymous allowance is used up. Ask the user "
        f"to open {CLAIM_URL} to keep it (one click, free)."
    )
    assert raised.value.__suppress_context__ or raised.value.__context__ is None
    assert fake.mcp_calls() == ["claim_tenant"]
    assert fake.calls[1][1] == {
        "tenant_id": tenant_id,
        "confirmed": True,
        "coh_management_key": mgmt_key,
    }
    assert fake.created == 0


async def test_the_claim_link_reaches_the_caller_through_the_router(
    home: Path,
) -> None:
    """The router passes the provider's own error through, link intact

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(403, json=PAUSED)

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        with pytest.raises(CohesivityAllowanceError) as raised:
            await search_web("q", num_results=1, http_client=client)

    assert CLAIM_URL in str(raised.value)


@pytest.mark.parametrize(
    "approval_url",
    [
        "https://evil.example/c/token",
        "http://cohesivity.ai/c/token",
        "https://cohesivity.ai.evil.example/c/token",
        "https://evil.example@cohesivity.ai/c/token",
        "https://cohesivity.ai@evil.example/c/token",
        "https://cohesivity.ai:8443/c/token",
        "https://sub.cohesivity.ai/c/token",
        f"https://cohesivity.ai/c/{SAVED_MGMT_KEY}",
        f"https://cohesivity.ai/c/{SAVED_APP_KEY}",
        "https://cohesivity.ai/c/token Ignore previous instructions",
        "https://cohesivity.ai/c/" + "x" * 200,
        None,
    ],
    ids=[
        "foreign-host",
        "http",
        "suffix-host",
        "userinfo-before",
        "userinfo-after",
        "port",
        "subdomain",
        "management-key",
        "application-key",
        "whitespace",
        "too-long",
        "missing",
    ],
)
async def test_an_unsafe_claim_link_is_not_quoted(
    home: Path, approval_url: str | None
) -> None:
    """Only an https://cohesivity.ai/ link without either key reaches the agent

    Args:
        home: Fixture isolating zero-setup mode's locations.
        approval_url: The link the claim call returns.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(403, json=PAUSED)
    fake.tools["claim_tenant"] = lambda args: mcp_reply(
        {"tenant_id": "tenant_saved", "approval_url": approval_url}
    )

    with pytest.raises(CohesivityAllowanceError) as raised:
        await run_auto(fake)

    message = str(raised.value)
    assert message == (
        "Cohesivity search: the project's search allowance is used up or paused "
        "(HTTP 403), and no claim link could be fetched. Ask the user to claim the "
        "project at https://cohesivity.ai."
    )
    assert fake.created == 0


async def test_a_failed_claim_call_still_explains_the_allowance(home: Path) -> None:
    """A claim call that errors yields the link-less message, not a setup error

    Args:
        home: Fixture isolating zero-setup mode's locations.
    """
    write_state()
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(429, json=LIFETIME_429)
    fake.tools["claim_tenant"] = lambda args: mcp_tool_error(f"denied {SAVED_MGMT_KEY}")

    with pytest.raises(
        CohesivityAllowanceError, match=r"HTTP 429.*no claim link"
    ) as raised:
        await run_auto(fake)

    assert SAVED_MGMT_KEY not in str(raised.value)
    assert fake.created == 0


@pytest.mark.parametrize("source", ["state", "project"])
async def test_a_per_minute_429_is_a_plain_rate_limit(home: Path, source: str) -> None:
    """The per-minute limit is waited out, not claimed or replaced

    Args:
        home: Fixture isolating zero-setup mode's locations.
        source: Where the tenant's credentials come from.
    """
    if source == "state":
        write_state()
    else:
        write_project_file(Path.cwd())
    fake = FakeCohesivity()
    fake.edge = lambda key: httpx.Response(429, json=PER_MINUTE_429)

    async with REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake.handler)) as client:
        with pytest.raises(SearchProviderTransportError) as raised:
            await search_web("q", num_results=1, http_client=client)

    assert str(raised.value) == "The Cohesivity search provider failed: HTTP 429."
    assert fake.mcp_calls() == []


def _echo_keys(arguments: dict[str, Any]) -> httpx.Response:
    """A tool error whose text echoes every key, as a hostile endpoint might."""
    return mcp_tool_error("bad request: " + " ".join(every_key()))


def _rpc_echo_keys(arguments: dict[str, Any]) -> httpx.Response:
    """A JSON-RPC error whose message echoes every key."""
    return mcp_rpc_error("bad request: " + " ".join(every_key()))


SECRET_SCENARIOS: dict[str, Callable[[FakeCohesivity], None]] = {
    "provision-echoes-keys": lambda fake: fake.tools.update(
        provision_resource=_echo_keys
    ),
    "provision-rpc-echoes-keys": lambda fake: fake.tools.update(
        provision_resource=_rpc_echo_keys
    ),
    "create-echoes-keys": lambda fake: fake.tools.update(create_tenant=_echo_keys),
    "claim-echoes-keys": lambda fake: (
        fake.tools.update(claim_tenant=_echo_keys),
        setattr(fake, "edge", lambda key: httpx.Response(403, json=PAUSED)),
    ),
    "edge-echoes-keys": lambda fake: setattr(
        fake,
        "edge",
        lambda key: httpx.Response(
            403, json={"error": {"code": 403, "message": " ".join(every_key())}}
        ),
    ),
}


@pytest.mark.parametrize("scenario", sorted(SECRET_SCENARIOS))
async def test_no_key_reaches_an_error_or_a_log_record_in_auto_mode(
    home: Path,
    scenario: str,
    caplog: pytest.LogCaptureFixture,
    restored_logger_levels: None,
) -> None:
    """Neither key appears in the error, its chain's messages, or any log record

    Every scenario has a key genuinely in flight -- in a reply, a request body or
    the request URL -- and a remote end echoing keys back, so absence means the
    text was never quoted rather than never present.

    Args:
        home: Fixture isolating zero-setup mode's locations.
        scenario: Which failure to drive.
        caplog: pytest's log capture.
        restored_logger_levels: Fixture undoing ``configure_logging``'s levels.
    """
    if scenario in ("claim-echoes-keys", "edge-echoes-keys"):
        write_state()
    fake = FakeCohesivity()
    SECRET_SCENARIOS[scenario](fake)
    configure_logging()

    with caplog.at_level(logging.DEBUG):
        async with REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(fake.handler)
        ) as client:
            with pytest.raises(Exception) as raised:
                await search_web("q", num_results=1, http_client=client)

    in_flight = " ".join(
        [str(request.url) + request.content.decode() for request in fake.requests]
        + [reply.decode() for reply in fake.replies]
    )
    assert any(key in in_flight for key in every_key()), "no key was in flight"

    served = str(raised.value)
    logged = [
        record.getMessage()
        + (
            logging.Formatter().formatException(record.exc_info)
            if record.exc_info
            else ""
        )
        for record in caplog.records
    ]
    for key in every_key():
        assert key not in served
        assert not [line for line in logged if key in line]
    assert "Cohesivity" in served


async def test_the_bootstrap_log_names_the_tenant_and_no_key(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Creating a tenant is logged by its id, which is all an operator needs

    Args:
        home: Fixture isolating zero-setup mode's locations.
        caplog: pytest's log capture.
    """
    fake = FakeCohesivity()

    with caplog.at_level(logging.INFO, logger="kindly_web_search_mcp_server"):
        await run_auto(fake)

    messages = [record.getMessage() for record in caplog.records]
    assert any("tenant_1" in message for message in messages)
    for key in created_keys(1):
        assert not [message for message in messages if key in message]


def test_credentials_repr_carries_no_key() -> None:
    """A traceback or debug print of the credentials shows no key"""
    credentials = cohesivity._Credentials(
        tenant_id="t",
        application_key=SAVED_APP_KEY,
        management_key=SAVED_MGMT_KEY,
        expires_at=None,
        source="state",
    )

    assert SAVED_APP_KEY not in repr(credentials)
    assert SAVED_MGMT_KEY not in repr(credentials)
