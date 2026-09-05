"""Transport-level failure paths for the six search-provider coroutines.

One module rather than six because the five failures pinned here -- ``401``,
``429``, a non-JSON body, a well-formed JSON body of the wrong shape, and a
timeout -- are identical in shape across every provider. Splitting per provider
would copy the :class:`httpx.MockTransport` helper six times and let the six
copies drift.

**What this module does and does not own.** It owns the contract every provider
owes its caller when the exchange fails: something is raised, it names the
provider or carries the status, and nothing is silently converted into "no
matches". It does **not** own each provider's parsing -- success shape,
malformed items, empty results -- which stays in that provider's own
``test_<name>_unit`` module. Nor does it own the *wording* of SearXNG's
per-status messages: those live beside SearXNG's other behaviour in
``test_searxng_unit.py``, because a message is that provider's own choice while
the contract here is shared by all six.

**Three things make these cases non-vacuous, and all three are needed.** Every
provider's ``*ConfigError`` subclasses its ``*Error``, so a case that loses its
credential raises the *expected base type* having sent no request at all. So each
case (a) runs on an environment cleared to nothing and built back up additively,
(b) asserts the **exact** exception class rather than a base class, and
(c) asserts the mocked transport was actually reached. Any one of the three alone
leaves a way to pass without an exchange.

**Statuses are read structurally, never out of the message.** SerpBase sends its
credential as a URL query parameter, so ``httpx``'s own
:class:`~httpx.HTTPStatusError` message quotes a URL containing the API key.
Asserting on that string would make the key part of a pinned expectation. See
``.system_design/TEST_SUITE.md`` section 14, which records the leak itself.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult
from kindly_web_search_mcp_server.search.searxng import SearxngError, search_searxng
from kindly_web_search_mcp_server.search.serpbase import SerpbaseError, search_serpbase
from kindly_web_search_mcp_server.search.serper import SerperError, search_serper
from kindly_web_search_mcp_server.search.sofya import SofyaError, search_sofya
from kindly_web_search_mcp_server.search.tavily import TavilyError, search_tavily
from kindly_web_search_mcp_server.search.youcom import YoucomError, search_youcom

SearchCoroutine = Callable[..., Awaitable[list[WebSearchResult]]]


@dataclass(frozen=True)
class ProviderCase:
    """Describe one provider well enough to drive its failure paths.

    Attributes:
        name: Short identifier, used as the parametrization id.
        search: The provider's search coroutine.
        env: The complete environment the provider runs under. Built onto a
            cleared environment, so it is the whole input, not a patch.
        body_error: Exact exception class raised for a body this provider cannot
            parse.
        status_error: Exact exception class raised for an HTTP error status.
            Five providers call ``raise_for_status()`` and let
            :class:`httpx.HTTPStatusError` out; SearXNG classifies the status
            itself and its per-instance loop then wraps the result.
        timeout_error: Exact exception class the caller sees when the transport
            times out. Five providers do not catch it, so the transport's own
            :class:`httpx.ReadTimeout` arrives; SearXNG's loop catches every
            exception and re-raises its aggregate.
    """

    name: str
    search: SearchCoroutine
    env: dict[str, str]
    body_error: type[Exception]
    status_error: type[Exception]
    timeout_error: type[Exception]


PROVIDER_CASES: tuple[ProviderCase, ...] = (
    ProviderCase(
        "serper",
        search_serper,
        {"SERPER_API_KEY": "test_key"},
        SerperError,
        httpx.HTTPStatusError,
        httpx.ReadTimeout,
    ),
    ProviderCase(
        "serpbase",
        search_serpbase,
        {"SERPBASE_API_KEY": "test_key"},
        SerpbaseError,
        httpx.HTTPStatusError,
        httpx.ReadTimeout,
    ),
    ProviderCase(
        "tavily",
        search_tavily,
        {"TAVILY_API_KEY": "test_key"},
        TavilyError,
        httpx.HTTPStatusError,
        httpx.ReadTimeout,
    ),
    ProviderCase(
        "searxng",
        search_searxng,
        {"SEARXNG_BASE_URL": "https://searx.example.org"},
        SearxngError,
        SearxngError,
        SearxngError,
    ),
    ProviderCase(
        "sofya",
        search_sofya,
        {"SOFYA_API_KEY": "test_key"},
        SofyaError,
        httpx.HTTPStatusError,
        httpx.ReadTimeout,
    ),
    ProviderCase(
        "youcom",
        search_youcom,
        {"YDC_API_KEY": "test_key"},
        YoucomError,
        httpx.HTTPStatusError,
        httpx.ReadTimeout,
    ),
)

CASE_IDS = tuple(case.name for case in PROVIDER_CASES)


def build_environment(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the process environment with exactly ``env``.

    Cleared and rebuilt rather than patched. Section 5.2b of
    ``.system_design/TEST_SUITE.md`` records why, measured: SearXNG alone reads
    nine variables at the point of use, so a case that deletes a remembered list
    of names is one name from being wrong. Concretely, an ambient
    ``SEARXNG_HEADERS_JSON`` makes ``search_searxng`` raise ``SearxngConfigError``
    -- a subclass of ``SearxngError`` -- before any request is sent, which would
    satisfy a base-class assertion with no exchange at all.

    **Measured on Linux only.** This removes every variable, ``PATH`` and
    ``SYSTEMROOT`` included, and this module is unmarked, so it will run in the
    ``fast`` job on Windows too once that lane exists. Nothing here consults the
    environment beyond the provider itself -- the transport is a double and opens
    no socket -- but an empty environment is a documented source of Windows
    surprises, so re-measure on the first Windows run rather than assuming.

    Args:
        env: The variables the provider under test needs, and nothing else.
        monkeypatch: pytest's environment patcher, which restores every removed
            and added variable when the test ends.
    """
    for name in list(os.environ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def status_code_of(error: BaseException) -> int | None:
    """Find the HTTP status behind an exception, following the chain.

    Read from :attr:`httpx.Response.status_code` rather than matched in the
    exception's message. ``httpx``'s message quotes the request URL, and SerpBase
    puts its API key in the query string, so a message assertion would pin a
    string containing a credential.

    Args:
        error: The exception the provider raised.

    Returns:
        The status code carried by the first :class:`httpx.HTTPStatusError` at
        or below ``error`` in the ``__cause__`` chain, or ``None`` when the chain
        holds none.
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code
        current = current.__cause__
    return None


async def run_against(
    case: ProviderCase,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    num_results: int = 3,
) -> list[WebSearchResult]:
    """Run one provider against a mocked transport.

    Args:
        case: The provider being driven.
        handler: Called with each outgoing request; returns the response, or
            raises to simulate a transport-level failure.
        num_results: Value forwarded to the provider.

    Returns:
        The provider's parsed results.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await case.search("q", num_results=num_results, http_client=client)


@pytest.mark.parametrize("status", (401, 429))
@pytest.mark.parametrize("case", PROVIDER_CASES, ids=CASE_IDS)
async def test_an_http_error_status_reaches_the_caller(
    case: ProviderCase, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail loudly on a rejected key (401) or a throttled account (429)

    These two are the failures an operator meets first and most often, and until
    this module existed no provider had a case for either.

    The body is a JSON error object because that is what these APIs actually
    return; it is not what makes the case non-vacuous. Measured on You.com: with
    ``raise_for_status()`` removed, a JSON error body still raises -- as
    ``YoucomError("... missing `results` object.")`` -- so a "does it raise"
    assertion would survive the mutation. What kills it is asserting the exact
    class, which this case does.
    """
    build_environment(case.env, monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": "rejected"})

    with pytest.raises(case.status_error) as excinfo:
        await run_against(case, handler)

    assert type(excinfo.value) is case.status_error
    assert status_code_of(excinfo.value) == status
    assert calls == 1


@pytest.mark.parametrize("case", PROVIDER_CASES, ids=CASE_IDS)
async def test_a_non_json_body_is_reported_as_the_providers_own_error(
    case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name the provider in the error when a 200 body will not parse

    An HTML body behind a 200 is what a captive portal or a misrouted proxy
    returns.

    The mutation this kills differs by provider, and the difference is measured.
    For five of them, removing the ``except ValueError`` arm lets a raw
    ``json.JSONDecodeError`` escape, and the class assertion alone fails the
    case. For SearXNG it does **not** escape: the per-instance loop catches it and
    re-raises the aggregate, which is still a ``SearxngError``. There, only the
    message assertion fails the case -- so that assertion is load-bearing, not
    decorative, and must not be dropped as redundant.
    """
    build_environment(case.env, monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html><body>not json</body></html>")

    with pytest.raises(case.body_error) as excinfo:
        await run_against(case, handler)

    assert type(excinfo.value) is case.body_error
    assert "not valid JSON" in str(excinfo.value)
    assert calls == 1


@pytest.mark.parametrize("case", PROVIDER_CASES, ids=CASE_IDS)
async def test_a_json_body_of_the_wrong_shape_is_rejected(
    case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a well-formed JSON body that is not an object

    A JSON array parses cleanly and then has no ``.get``. Without the
    ``isinstance(data, dict)`` guard an :class:`AttributeError` escapes from the
    first lookup, which reads as a bug in this server rather than as a response
    the provider should not have sent.
    """
    build_environment(case.env, monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(case.body_error) as excinfo:
        await run_against(case, handler)

    assert type(excinfo.value) is case.body_error
    assert "not a JSON object" in str(excinfo.value)
    assert calls == 1


@pytest.mark.parametrize("case", PROVIDER_CASES, ids=CASE_IDS)
async def test_a_transport_timeout_reaches_the_caller(
    case: ProviderCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface a timeout rather than converting it into "no matches"

    SearXNG has a branch here -- its per-instance loop catches every exception --
    and removing it fails this case. **The other five providers have no branch to
    remove**, so for them this is a regression guard rather than a mutation
    target: it fails the day someone wraps the request in
    ``except Exception: return []``, which would turn an operator-visible timeout
    into an empty result set indistinguishable from a query with no hits. Stated
    rather than left implicit, because a mutation run will report nothing for
    those five rows and the next reader would otherwise read that as a hole.

    The timeout is injected, so this pins **propagation**, not **arming**. Whether
    a provider sets a deadline at all is a separate claim, and for SearXNG --
    which by default sets none -- it is asserted in ``test_searxng_unit.py``.
    """
    build_environment(case.env, monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(case.timeout_error) as excinfo:
        await run_against(case, handler)

    assert type(excinfo.value) is case.timeout_error
    assert calls == 1


async def test_the_searxng_aggregate_chains_to_the_last_instance_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the failing instance's own error reachable under the aggregate

    ``search_searxng`` tries each configured instance and, when all of them fail,
    raises one aggregate error. The per-instance error is what an operator can
    act on -- the 403 arm exists to tell them to enable the ``json`` format on
    their instance -- so it must survive as an exception with a traceback, not
    only as a substring of the aggregate's message.

    This is the case the production change in this step was made for. Before it,
    the aggregate arrived with **both** ``__cause__`` and ``__context__`` unset:
    the ``raise`` sits outside the ``except`` block, so implicit chaining does not
    reach it either.

    The two instances answer differently on purpose. Asserting the chain carries
    the **last** failure is what fails a ``last_error`` that is assigned once and
    never updated -- a mutation the message alone cannot see, since either
    instance's error would read plausibly in it.
    """
    build_environment(
        {"SEARXNG_BASE_URL": "https://first.example,https://last.example"}, monkeypatch
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        if host == "first.example":
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(SearxngError) as excinfo:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await search_searxng("q", num_results=3, http_client=client)

    # Every instance is tried before the aggregate is raised.
    assert seen == ["first.example", "last.example"]

    aggregate = excinfo.value
    assert "All configured SearXNG instances failed" in str(aggregate)

    # The last instance's own error, with its operator instruction intact.
    cause = aggregate.__cause__
    assert isinstance(cause, SearxngError)
    assert "enable the 'json' format" in str(cause)

    # And the HTTP error underneath it, which is what carries the status.
    assert status_code_of(cause) == 403
