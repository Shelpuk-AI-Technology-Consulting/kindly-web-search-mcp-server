"""No search-provider credential reaches the MCP client inside a tool error.

For this server the MCP client is an LLM agent, so anything returned from a tool
lands in a model provider's request and in the conversation transcript. That
makes a credential in a tool error message a disclosure to a third party, not a
local log entry, and it is why this invariant is asserted at the tool boundary
rather than only where the message is built.

**Two layers, because neither alone can prove the claim.**

The unit layer pins the conversion in
:func:`~kindly_web_search_mcp_server.search.search_web`: an ``httpx`` failure
becomes an exception whose message is built from the provider's label and the
HTTP status, with the request URL dropped. The contract layer drives the
low-level ``CallToolRequest`` handler rather than ``mcp.call_tool``, because
``call_tool`` raises FastMCP's ``ToolError`` and the served text and ``isError``
are composed one layer below it -- so no unit test of the conversion, and no
assertion on the ``ToolError``, can prove what the client actually sees. Measured
on ``mcp`` 1.29.1 and ``httpx`` 0.28.1: the served ``CallToolResult`` carries
``str(exc)`` alone and exposes neither ``__cause__`` nor a traceback -- so a
future release that began rendering ``__cause__`` at *either* layer turns these
cases red, which is the outcome that should follow.

**Why the URL is dropped rather than redacted.** SerpBase authenticates by query
parameter, SearXNG by base-URL userinfo, and a future provider may use a
parameter name nobody listed. Stripping parameters named ``api_key``, ``key`` or
``token`` is a denylist that fails open and silently on the first name outside
it. Dropping the URL has no such gap, and needs no pattern to be kept current.

**What makes the seven-provider sweep non-vacuous.** Only SerpBase disclosed on the
unrepaired tree; the other six already passed, so on their own they are
regression cover and not evidence. Pointed at a header-authenticating provider,
a "the secret is absent" assertion passes while proving nothing. So a **sibling
case** asserts, once per provider, that the credential was genuinely *in flight*
-- in the request URL for the two providers that carry it there, in a request
header for the five that do not. A provider that stopped being configured, or
was swapped for one that never disclosed, fails that control instead of passing
quietly. The sweep rows themselves assert absence only; the control is what makes
their absence mean something.

``build_environment`` is imported from ``test_search_provider_error_paths``
rather than copied. Its docstring records the measured reason an environment has
to be cleared and rebuilt rather than patched -- SearXNG alone reads nine
variables at the point of use -- and two copies of that reasoning would drift.
Renaming it there breaks this module too.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.types import CallToolRequest, CallToolRequestParams

from kindly_web_search_mcp_server.search import (
    SearchProviderTransportError,
    WebSearchProviderError,
    search_web,
)
from kindly_web_search_mcp_server.search.searxng import SearxngConfigError, SearxngError
from kindly_web_search_mcp_server.search.serpbase import SerpbaseError
from tests.test_search_provider_error_paths import build_environment, status_code_of

#: Captured before any case rebinds :class:`httpx.AsyncClient`. Binding the
#: double to the name it is about to replace makes each replacement subclass the
#: previous one, and the outermost transport is then overwritten by the innermost
#: -- every case answers with the first case's status. Observed while measuring
#: this defect, which is why the real class is held here instead.
REAL_ASYNC_CLIENT = httpx.AsyncClient

#: Lower-case on purpose. An upper-case token with an underscore matches the
#: shape ``test_baseline_failure_ledger`` treats as an environment-variable name,
#: which would enlist this module in a sweep it has no part in.
SENTINEL = "canary-not-a-real-credential"


@dataclass(frozen=True)
class DisclosureCase:
    """Describe one provider well enough to prove its credential stays private.

    Attributes:
        name: Short identifier, used as the parametrization id.
        label: The human-readable name the repaired error message must carry.
        env: The complete environment that selects this provider, built onto a
            cleared environment rather than patched onto the ambient one.
        secret: The exact substring that must never reach the MCP client.
        carried_in_url: ``True`` when this provider puts its credential in the
            request URL -- SerpBase in a query parameter, SearXNG in the base
            URL's userinfo. ``False`` when it uses a header. This decides which
            in-flight control the case asserts, and is what stops a row passing
            because it silently stopped sending a credential at all.
    """

    name: str
    label: str
    env: dict[str, str]
    secret: str
    carried_in_url: bool


DISCLOSURE_CASES: tuple[DisclosureCase, ...] = (
    DisclosureCase(
        "serper",
        "Serper",
        {"SERPER_API_KEY": f"serper-{SENTINEL}"},
        f"serper-{SENTINEL}",
        False,
    ),
    DisclosureCase(
        "serpbase",
        "SerpBase",
        {"SERPBASE_API_KEY": f"serpbase-{SENTINEL}"},
        f"serpbase-{SENTINEL}",
        True,
    ),
    DisclosureCase(
        "tavily",
        "Tavily",
        {"TAVILY_API_KEY": f"tavily-{SENTINEL}"},
        f"tavily-{SENTINEL}",
        False,
    ),
    DisclosureCase(
        "searxng",
        "SearXNG",
        {"SEARXNG_BASE_URL": f"https://operator:{SENTINEL}@searx.example.org"},
        SENTINEL,
        True,
    ),
    DisclosureCase(
        "sofya",
        "Sofya",
        {"SOFYA_API_KEY": f"sofya-{SENTINEL}"},
        f"sofya-{SENTINEL}",
        False,
    ),
    DisclosureCase(
        "youcom",
        "You.com",
        {"YDC_API_KEY": f"ydc-{SENTINEL}"},
        f"ydc-{SENTINEL}",
        False,
    ),
    DisclosureCase(
        "serply",
        "Serply",
        {"SERPLY_API_KEY": f"serply-{SENTINEL}"},
        f"serply-{SENTINEL}",
        False,
    ),
)

CASE_IDS = tuple(case.name for case in DISCLOSURE_CASES)

#: The statuses an operator actually meets: an expired or mistyped key, a quota
#: response, and a provider-side fault.
STATUSES = (401, 429, 500)

SERPBASE = next(case for case in DISCLOSURE_CASES if case.name == "serpbase")


def recording_client_class(
    status: int, sent: list[httpx.Request]
) -> type[httpx.AsyncClient]:
    """Build an ``AsyncClient`` subclass that answers every request with ``status``

    The router constructs its own client, so there is no argument to inject a
    transport through at the tool boundary. Subclassing and forcing ``transport``
    reaches that construction without the router needing a seam it does not have.

    Args:
        status: The HTTP status every request is answered with.
        sent: Accumulator the handler appends each received request to, so a case
            can assert what was actually put on the wire.

    Returns:
        A class usable in place of :class:`httpx.AsyncClient`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json={"error": "denied"})

    class _RecordingClient(REAL_ASYNC_CLIENT):  # type: ignore[valid-type,misc]
        """An ``AsyncClient`` whose transport is always the recording double."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """Force the recording transport regardless of what the caller asked for.

            Args:
                *args: Positional arguments forwarded to :class:`httpx.AsyncClient`.
                **kwargs: Keyword arguments forwarded likewise, with ``transport``
                    replaced.
            """
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    return _RecordingClient


async def call_the_tool(
    case: DisclosureCase,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, list[httpx.Request]]:
    """Drive ``web_search`` and return the payload an MCP client is served.

    Reaches the low-level ``CallToolRequest`` handler rather than
    :meth:`~mcp.server.fastmcp.FastMCP.call_tool`. ``call_tool`` raises FastMCP's
    ``ToolError``; the served text and ``isError`` are composed one layer below
    it, so asserting on the ``ToolError`` would leave a change at that lower
    layer invisible to every case here.

    Args:
        case: The provider to configure and drive.
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's patcher, which restores the environment and the
            rebound client class when the case ends.

    Returns:
        A pair of the text the MCP client receives -- the raised error rendered
        as the client would see it, or the successful payload -- and the requests
        the transport observed.
    """
    from kindly_web_search_mcp_server.server import mcp

    sent: list[httpx.Request] = []
    build_environment(case.env, monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", recording_client_class(status, sent))

    # `_mcp_server` is the only way to observe the served payload; see the docstring.
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="web_search", arguments={"query": "q", "num_results": 1}
        ),
    )
    served = await mcp._mcp_server.request_handlers[CallToolRequest](request)

    # Both shapes are searched: a secret reaches a client in a payload as easily as in an error.
    result = served.root
    text = " ".join(getattr(block, "text", "") for block in result.content)
    return f"isError={result.isError} {text}", sent


async def drive_router(
    case: DisclosureCase,
    handler: object,
) -> tuple[BaseException, list[httpx.Request]]:
    """Run the router against a mocked transport and return what it raised.

    Args:
        case: The provider whose environment is already in place.
        handler: A ``MockTransport`` handler, which may return a response or
            raise to simulate a transport failure.

    Returns:
        A pair of the exception :func:`search_web` raised and the requests the
        transport observed, so a case asserting a secret is *absent* from the
        message can first establish it was present on the wire.

    Raises:
        AssertionError: If the router returned instead of raising, which would
            mean the case proved nothing.
    """
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)  # type: ignore[operator]

    transport = httpx.MockTransport(recording)
    async with REAL_ASYNC_CLIENT(transport=transport) as client:
        try:
            await search_web("q", num_results=1, http_client=client)
        except BaseException as exc:  # noqa: BLE001 - the subject of every case
            return exc, sent
    raise AssertionError("search_web returned where the case required it to raise")


# --------------------------------------------------------------------------
# The conversion itself.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", STATUSES)
async def test_an_http_status_is_reported_by_provider_and_status(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converted error names the provider and the status it received.

    Dropping the URL is only half the repair: an error that says nothing is not
    a fix, because the operator still has to learn that their key was rejected.

    Args:
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    raised, _sent = await drive_router(
        SERPBASE, lambda request: httpx.Response(status, json={"error": "denied"})
    )

    assert type(raised) is SearchProviderTransportError
    assert SERPBASE.label in str(raised)
    assert str(status) in str(raised)


async def test_the_message_names_the_provider_by_its_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label reaches the client, not the internal short name.

    ``PROVIDERS`` carries both; ``label`` is the one the dataclass documents as
    belonging in error messages. Asserting the label alone would still pass if
    the short name were used for a provider whose two spellings coincide, so the
    short name is asserted absent as well. ``SerpBase`` and ``serpbase`` differ
    only in case, so the comparison is made case-sensitively on purpose.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    raised, _sent = await drive_router(
        SERPBASE, lambda request: httpx.Response(401, json={"error": "denied"})
    )

    assert "SerpBase" in str(raised)
    assert "serpbase" not in str(raised)


@pytest.mark.parametrize("status", STATUSES)
async def test_the_message_carries_neither_the_credential_nor_the_url(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converted message holds no part of the request URL.

    The host is asserted absent as well as the key. A message that had dropped
    the query string but kept the host would pass a key-only assertion while
    leaving the mechanism -- ``httpx`` quoting the URL -- in place. And the
    credential is asserted *present on the wire* first, because an absence
    assertion over a provider that never sends one proves nothing.

    Args:
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    raised, sent = await drive_router(
        SERPBASE, lambda request: httpx.Response(status, json={"error": "denied"})
    )

    # The absence is evidence only if the secret was there to be dropped.
    assert SERPBASE.secret in str(sent[0].url)
    assert SERPBASE.secret not in str(raised)
    assert "api.serpbase.dev" not in str(raised)


@pytest.mark.parametrize("status", STATUSES)
async def test_the_original_status_error_survives_as_the_cause(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status stays readable structurally through ``__cause__``.

    The URL is removed from what the caller is *told*, not from what the process
    holds. Keeping the chain is what lets a status be read without parsing a
    message -- the practice ``test_search_provider_error_paths`` relies on.

    Args:
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    raised, _sent = await drive_router(
        SERPBASE, lambda request: httpx.Response(status, json={"error": "denied"})
    )

    assert status_code_of(raised) == status


async def test_a_transport_failure_without_a_status_is_also_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout carries no status, and must still lose its URL.

    Only :class:`httpx.HTTPStatusError` was measured to quote the URL in its
    message today. The other :class:`httpx.HTTPError` subclasses -- ``ReadTimeout``,
    ``ConnectTimeout``, ``ConnectError``, ``ReadError``, ``RemoteProtocolError`` --
    reach the URL only through ``.request.url``, which nothing renders. The
    conversion covers the whole family anyway, so an upstream change to any of
    those messages cannot reopen the disclosure. This case is what holds that
    wider catch in place.

    **What the catch deliberately does not cover**, measured on ``httpx`` 0.28.1:
    ``InvalidURL``, ``CookieConflict`` and ``StreamError`` are **not**
    ``HTTPError`` subclasses, and ``InvalidURL`` carries no ``.request`` at all.
    They are unreachable from the five providers that build a URL from a constant.
    A provider added later that derives its URL from configuration would inherit
    an uncaught family, which is worth knowing before writing one.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    raised, _sent = await drive_router(SERPBASE, time_out)

    assert type(raised) is SearchProviderTransportError
    assert SERPBASE.label in str(raised)
    assert "ReadTimeout" in str(raised)
    assert SERPBASE.secret not in str(raised)


async def test_a_providers_own_error_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``httpx`` provider failure keeps its class and its message.

    The conversion is aimed at the one family that quotes a URL. Provider-authored
    messages carry no URL and are frequently the more useful text, so widening the
    catch to every exception would cost the caller information for no gain. This
    case fails if the ``except`` is widened to bare ``Exception``.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    build_environment(SERPBASE.env, monkeypatch)

    raised, _sent = await drive_router(
        SERPBASE, lambda request: httpx.Response(200, text="not json at all")
    )

    assert type(raised) is SerpbaseError
    assert "not valid JSON" in str(raised)


# --------------------------------------------------------------------------
# What the MCP client actually receives.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("case", DISCLOSURE_CASES, ids=CASE_IDS)
async def test_no_providers_credential_reaches_the_mcp_client(
    case: DisclosureCase, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eighteen rows: no configured credential appears in what the client sees.

    Three of these -- SerpBase's -- failed before the repair; the other fifteen
    passed and are regression cover, so that a later change cannot move the
    disclosure to a provider nobody was watching.

    Args:
        case: The provider being driven.
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's environment patcher.
    """
    client_text, _sent = await call_the_tool(case, status, monkeypatch)

    assert case.secret not in client_text


@pytest.mark.parametrize("case", DISCLOSURE_CASES, ids=CASE_IDS)
async def test_each_case_actually_puts_its_credential_on_the_wire(
    case: DisclosureCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that stops the sweep above from passing vacuously.

    An absence assertion proves nothing unless the thing was present to begin
    with. A row pointed at a provider that never sends a credential -- or one
    whose environment silently stopped selecting it -- would pass while asserting
    nothing at all. This case pins that the credential really was in flight, in
    the place that provider puts it.

    Args:
        case: The provider being driven.
        monkeypatch: pytest's environment patcher.
    """
    _client_text, sent = await call_the_tool(case, 401, monkeypatch)

    assert len(sent) >= 1, "the transport was never reached, so nothing was proven"
    request = sent[0]
    if case.carried_in_url:
        assert case.secret in str(request.url)
    else:
        assert any(case.secret in value for value in request.headers.values())


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("case", DISCLOSURE_CASES, ids=CASE_IDS)
async def test_the_client_is_told_the_provider_and_the_status(
    case: DisclosureCase, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client still learns which provider failed and how.

    Paired with the sweep above so the repair cannot be satisfied by returning
    nothing useful. SearXNG composes its own message and is asserted on its own
    terms: it names the status without ever naming the URL, which is why it never
    disclosed in the first place.

    Args:
        case: The provider being driven.
        status: The HTTP status the transport answers with.
        monkeypatch: pytest's environment patcher.
    """
    client_text, _sent = await call_the_tool(case, status, monkeypatch)

    assert case.label in client_text
    assert str(status) in client_text


async def test_an_unconfigured_server_still_tells_the_client_what_to_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-provider message is not collateral damage of the repair.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    client_text, sent = await call_the_tool(
        DisclosureCase("none", "", {}, SENTINEL, False), 401, monkeypatch
    )

    assert sent == []
    assert "SERPBASE_API_KEY" in client_text
    assert "No web search provider is configured" in client_text


def test_the_router_raises_a_type_of_its_own_for_a_configuration_failure() -> None:
    """The two router-level errors are siblings, not a base and its subclass.

    Every provider's ``*ConfigError`` subclasses its ``*Error``, and this suite
    has already been bitten by that: a case that loses its credential raises the
    expected base type having sent no request, satisfying an assertion aimed at a
    transport failure. Keeping these two unrelated means ``isinstance`` and an
    exact-class check cannot disagree here.
    """
    assert not issubclass(SearchProviderTransportError, WebSearchProviderError)
    assert not issubclass(WebSearchProviderError, SearchProviderTransportError)


async def test_a_malformed_base_url_is_not_echoed_back_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SearXNG base URL that fails to parse must not be quoted back.

    A second disclosure of the same class, found while repairing the first and
    measured the same way. ``SEARXNG_BASE_URL`` carries its credential in the
    URL's userinfo, and the "no valid URLs" message quoted the raw value -- so an
    operator who mistypes the scheme, the commonest way to get this message,
    hands the password to the client. The repaired message names the variable and
    the required shape instead, which is the more useful text as well as the safe
    one.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    build_environment(
        {"SEARXNG_BASE_URL": f"operator:{SENTINEL}@searx.example.org"}, monkeypatch
    )

    with pytest.raises(SearxngConfigError) as raised:
        await search_web("q", num_results=1)

    assert SENTINEL not in str(raised.value)
    assert "SEARXNG_BASE_URL" in str(raised.value)


async def test_no_log_record_carries_a_searxng_credential(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The instance URL is logged with its userinfo stripped, on both log sites.

    The exception path and the log path are siblings, and repairing one without
    the other leaves the credential exactly as exposed: under the stdio
    transport the server's stderr is read by the MCP host, so a log line is a
    persisted disclosure rather than a local one. The repository's own provider
    rules say so directly -- never in a log line.

    Both sites are driven at once because both fire on one failed attempt: the
    ``INFO`` line naming the instance about to be queried, and the ``WARNING``
    naming the one that failed.

    Args:
        monkeypatch: pytest's environment patcher.
        caplog: pytest's log capture, set to ``INFO`` so the quieter site is
            captured too -- at the shipped default it would not be emitted, and
            the case would pass without ever seeing it.
    """
    import logging

    case = next(c for c in DISCLOSURE_CASES if c.name == "searxng")
    with caplog.at_level(logging.INFO):
        _client_text, sent = await call_the_tool(case, 401, monkeypatch)

    assert sent, "no request was attempted, so no log site was reached"
    assert caplog.records, "nothing was logged, so the assertion below is vacuous"
    assert not [r for r in caplog.records if SENTINEL in r.getMessage()]


async def test_a_rejected_base_url_entry_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An entry rejected for having no scheme is reported by position, not value.

    This is the log site ``redact_url_credentials`` cannot repair, and the reason
    is worth keeping: that helper matches ``://user:pass@``, while an entry
    reaches this branch precisely because it has no usable scheme. Measured --
    passing the rejected value through the helper returns it unchanged. So the
    value is dropped, as it is in the exception path, rather than filtered.

    Args:
        monkeypatch: pytest's environment patcher.
        caplog: pytest's log capture.
    """
    import logging

    build_environment(
        {"SEARXNG_BASE_URL": f"operator:{SENTINEL}@searx.example.org"}, monkeypatch
    )

    with caplog.at_level(logging.INFO), pytest.raises(SearxngConfigError):
        await search_web("q", num_results=1)

    assert caplog.records, "nothing was logged, so the assertion below is vacuous"
    assert not [r for r in caplog.records if SENTINEL in r.getMessage()]
    assert any("SEARXNG_BASE_URL" in r.getMessage() for r in caplog.records)


async def test_a_logged_exception_message_is_redacted_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure's own message is redacted, not just the instance URL.

    ``search_searxng`` logs two operands, and only one of them is the base URL.
    The other is whatever the client raised, which is a value derived from the
    request -- and the rule this repository now states is that anything derived
    from a request URL is credential-bearing until shown otherwise. No reachable
    ``httpx`` message quotes a URL today, so there is no live disclosure; this
    case exists so that the redaction cannot be removed as apparently redundant.

    Unlike the rejected-entry site, the helper *does* work here: a message that
    quoted a URL would be quoting one that carried a scheme.

    Args:
        monkeypatch: pytest's environment patcher.
        caplog: pytest's log capture.
    """
    import logging

    case = next(c for c in DISCLOSURE_CASES if c.name == "searxng")
    build_environment(case.env, monkeypatch)

    def fail_loudly(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    with caplog.at_level(logging.INFO):
        raised, _sent = await drive_router(case, fail_loudly)

    # `drive_router` returns the exception rather than raising it, so the class
    # is asserted directly.
    assert type(raised) is SearxngError

    quoted = [r for r in caplog.records if "failed connecting to" in r.getMessage()]
    assert quoted, "the exception was never logged, so the assertion below is vacuous"
    assert not [r for r in quoted if SENTINEL in r.getMessage()]


async def test_the_searxng_aggregate_does_not_quote_a_credentialed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate served to the client redacts the failure it quotes.

    ``search_searxng`` interpolates its last per-instance failure into one
    aggregate message, and that message is provider-authored -- so the router's
    conversion passes it through untouched, ``SearxngError`` not being an
    ``httpx.HTTPError``. It is therefore the one client-visible surface where an
    httpx-formatted string survives, and redacting the *log* copy of the same
    operand while leaving this one raw would fix the less exposed of the two.

    No reachable ``httpx`` message quotes a credential today, so this is not a
    live disclosure; the case drives an injected failure that does quote one, so
    that the redaction cannot be removed as apparently redundant.

    Args:
        monkeypatch: pytest's environment patcher.
    """
    case = next(c for c in DISCLOSURE_CASES if c.name == "searxng")
    build_environment(case.env, monkeypatch)

    def fail_loudly(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    raised, sent = await drive_router(case, fail_loudly)

    assert sent, "no request was attempted, so nothing was quoted"
    assert type(raised) is SearxngError
    assert "failed connecting to" in str(raised), "the message was not quoted at all"
    assert SENTINEL not in str(raised)


@pytest.mark.parametrize(
    ("name", "inside_the_family"),
    (
        ("HTTPStatusError", True),
        ("ReadTimeout", True),
        ("ConnectError", True),
        ("UnsupportedProtocol", True),
        ("InvalidURL", False),
        ("CookieConflict", False),
        ("StreamError", False),
    ),
)
def test_the_httpx_error_family_has_the_shape_the_conversion_assumes(
    name: str, inside_the_family: bool
) -> None:
    """Pin the class boundary the router's single ``except`` clause rests on.

    ``search_web`` catches ``httpx.HTTPError`` and nothing else, so which classes
    fall inside that family *is* the scope of the repair. Both directions are
    asserted, because each answers a different question. The four inside are the
    ones the conversion must cover -- if any left the family, a provider failure
    would reach the client unconverted. The three outside are the documented
    limit: ``InvalidURL``, ``CookieConflict`` and ``StreamError`` derive from
    ``Exception`` and ``RuntimeError``, not from ``HTTPError``.

    Written because that limit was recorded as prose in a docstring and in
    ``.system_design/TEST_SUITE.md`` section 14, scoped to a version, against a
    dependency declared as a **range** -- ``httpx[socks]>=0.28,<1`` in
    ``pyproject.toml``. ``requirements-ratchet.txt`` pins ``0.28.1``, but that
    governs one job rather than what an ordinary install resolves, so an ``httpx``
    upgrade could still have made the documentation quietly false with nothing to
    notice. It is a claim about a third party, which is exactly the kind that
    decays without a case holding it.

    Args:
        name: The ``httpx`` attribute naming the exception class.
        inside_the_family: Whether it is expected to subclass ``httpx.HTTPError``.
    """
    error_class = getattr(httpx, name)

    assert issubclass(error_class, httpx.HTTPError) is inside_the_family


def test_invalid_url_carries_no_request_to_read_a_url_from() -> None:
    """The one outside the family that a configuration-derived URL can reach.

    SearXNG builds its URL from ``SEARXNG_BASE_URL``, so unlike the five
    constant-URL providers it can raise ``InvalidURL`` -- and being outside the
    family, the router never sees it. What keeps that harmless is asserted here:
    the exception carries no ``request``, so there is no URL on it for anything
    downstream to render. Section 14 records that SearXNG's own blanket
    ``except`` is what actually absorbs it.
    """
    assert not hasattr(httpx.InvalidURL("Invalid port: 'notaport'"), "request")
