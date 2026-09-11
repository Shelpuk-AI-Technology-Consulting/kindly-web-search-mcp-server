"""Search providers (Serper → SerpBase → Tavily → SearXNG → Sofya → You.com → Serply).

:data:`PROVIDERS` is the single source of truth for which providers exist, what
configures them, and the order they are selected in. Adding a provider means
appending one entry here; the router, the startup preflight check in
:mod:`~kindly_web_search_mcp_server.server`, and its diagnostics snapshot all read
from it, and tests assert the documentation matches it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from ..models import WebSearchResult
from ..utils.diagnostics import Diagnostics
from .searxng import search_searxng
from .serpbase import search_serpbase
from .serper import search_serper
from .serply import search_serply
from .sofya import search_sofya
from .tavily import search_tavily
from .youcom import search_youcom


# The provider coroutines are re-exported deliberately. `SearchProviderSpec` resolves
# them by attribute name at call time rather than holding a reference, so a static
# reader (and the linter) sees no use for the imports above -- but rebinding these
# module attributes is exactly how tests substitute providers.
__all__ = [
    "PROVIDERS",
    "SearchProviderSpec",
    "SearchProviderTransportError",
    "WebSearchProviderError",
    "any_provider_configured",
    "provider_env_vars",
    "search_searxng",
    "search_serpbase",
    "search_serper",
    "search_serply",
    "search_sofya",
    "search_tavily",
    "search_web",
    "search_youcom",
]


class WebSearchProviderError(RuntimeError):
    """Report that no search provider is configured at all.

    A configuration fault rather than a request fault: nothing was attempted and
    no provider was selected. Kept distinct from
    :class:`SearchProviderTransportError`, which reports a selected provider's
    request failing -- see that class for why the two are siblings rather than a
    base and its subclass.
    """


class SearchProviderTransportError(RuntimeError):
    """Report a provider's HTTP failure without quoting the request URL.

    Raised in place of any :class:`httpx.HTTPError` a provider lets out.
    :meth:`httpx.HTTPStatusError.__str__` quotes the full request URL, and at
    least one provider -- SerpBase -- authenticates with a query parameter, so
    the unmodified message carries an API key. Because that message is rendered
    into the error a FastMCP tool returns, the key would reach the MCP client,
    which for this server is an LLM agent.

    The URL is dropped rather than filtered. Stripping parameters whose names
    look credential-shaped is a denylist, and it fails open and silently on the
    first provider that names its parameter something else. The original
    exception stays reachable as ``__cause__``, so the status remains readable
    structurally even though it is no longer parsed out of a message.

    Deliberately **not** a subclass of :class:`WebSearchProviderError`, which
    means "no provider is configured". Every provider's ``*ConfigError`` already
    subclasses its ``*Error``, and that relationship has let a test satisfy an
    assertion aimed at a transport failure while sending no request at all.
    Sibling types keep an ``isinstance`` check and an exact-class check in
    agreement.
    """


@dataclass(frozen=True)
class SearchProviderSpec:
    """Describe one search provider and how to reach it.

    Attributes:
        name: Short identifier reported as ``provider`` in diagnostics.
        label: Human-readable name used in documentation and error messages.
        env_var: Environment variable whose presence selects this provider.
        function_name: Attribute name of this provider's search coroutine in
            this module.
        diagnostics_key: Key used for this provider's flag in the
            ``search.provider_select`` diagnostics payload.
    """

    name: str
    label: str
    env_var: str
    function_name: str
    diagnostics_key: str

    def is_configured(self) -> bool:
        """Report whether this provider's environment variable is set.

        Returns:
            ``True`` when the variable holds a non-blank value.
        """
        return bool(os.environ.get(self.env_var, "").strip())

    def search_function(self) -> Callable[..., Awaitable[list[WebSearchResult]]]:
        """Resolve this provider's search coroutine.

        Looked up by name at call time rather than captured as a reference when
        :data:`PROVIDERS` is built. Tests patch these coroutines by module
        attribute (``patch("...search.search_serper")``), which rebinds the module
        attribute; a captured reference would silently keep pointing at the
        original function and bypass the patch.

        Returns:
            The coroutine function that queries this provider.
        """
        return getattr(sys.modules[__name__], self.function_name)


# Order is the selection order: the first configured provider wins, with no
# cross-provider fallback. `searxng` keeps a `_config` diagnostics key rather than
# `_key` because it is configured by a base URL, not an API key.
PROVIDERS: tuple[SearchProviderSpec, ...] = (
    SearchProviderSpec(
        "serper", "Serper", "SERPER_API_KEY", "search_serper", "has_serper_key"
    ),
    SearchProviderSpec(
        "serpbase",
        "SerpBase",
        "SERPBASE_API_KEY",
        "search_serpbase",
        "has_serpbase_key",
    ),
    SearchProviderSpec(
        "tavily", "Tavily", "TAVILY_API_KEY", "search_tavily", "has_tavily_key"
    ),
    SearchProviderSpec(
        "searxng", "SearXNG", "SEARXNG_BASE_URL", "search_searxng", "has_searxng_config"
    ),
    SearchProviderSpec(
        "sofya", "Sofya", "SOFYA_API_KEY", "search_sofya", "has_sofya_key"
    ),
    SearchProviderSpec(
        "youcom", "You.com", "YDC_API_KEY", "search_youcom", "has_youcom_key"
    ),
    SearchProviderSpec(
        "serply", "Serply", "SERPLY_API_KEY", "search_serply", "has_serply_key"
    ),
)


def any_provider_configured() -> bool:
    """Report whether at least one search provider is configured.

    Returns:
        ``True`` when any provider's environment variable is set.
    """
    return any(provider.is_configured() for provider in PROVIDERS)


def provider_env_vars() -> tuple[str, ...]:
    """List the environment variables that select a search provider.

    Returns:
        The variables in provider selection order.
    """
    return tuple(provider.env_var for provider in PROVIDERS)


def _without_request_url(
    label: str, error: httpx.HTTPError
) -> SearchProviderTransportError:
    """Rebuild a provider's HTTP failure as a message safe to return to a client

    Args:
        label: The provider's human-readable name, taken from its registry entry
            so the message and the diagnostics cannot name different providers.
        error: The failure the provider let out.

    Returns:
        An exception naming the provider and, when the failure carries one, the
        HTTP status -- and nothing else. Never the URL, and so never a credential
        the URL happens to carry.
    """
    # A status is the actionable half of the message for an operator: it
    # separates a rejected key from a quota from a provider-side fault. Failures
    # with no response -- timeouts, connection errors -- have only their class.
    if isinstance(error, httpx.HTTPStatusError):
        detail = f"HTTP {error.response.status_code}"
    else:
        detail = type(error).__name__
    return SearchProviderTransportError(
        f"The {label} search provider failed: {detail}."
    )


async def search_web(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
    diagnostics: Diagnostics | None = None,
) -> list[WebSearchResult]:
    """Search the web using the first configured provider in :data:`PROVIDERS`

    Selection is by strict priority order with no cross-provider fallback: the
    first provider whose environment variable is set handles the query, and a
    failure from it is raised rather than retried against another provider.

    Args:
        query: The search query to run.
        num_results: Maximum number of results to return.
        http_client: Client to reuse for the request. A short-lived client is
            created when omitted.
        diagnostics: Sink for the provider-selection diagnostic. Nothing is
            emitted when omitted.

    Returns:
        The provider's results, at most ``num_results`` of them.

    Raises:
        WebSearchProviderError: If no provider is configured.
        SearchProviderTransportError: If the selected provider's request fails at
            the HTTP layer. Replaces the ``httpx`` exception rather than letting
            it out, because that exception's message quotes the request URL --
            which for SerpBase carries the API key. The original is chained as
            ``__cause__``.
    """
    # Read each provider's configuration once, so the selection and the emitted
    # diagnostic cannot disagree if the environment changes mid-call.
    statuses = [(provider, provider.is_configured()) for provider in PROVIDERS]

    selected = next((provider for provider, ok in statuses if ok), None)
    if selected is None:
        variables = ", ".join(provider_env_vars())
        raise WebSearchProviderError(
            f"No web search provider is configured. Set one of: {variables}."
        )

    provider_fn: Callable[..., Awaitable[list[WebSearchResult]]] = (
        selected.search_function()
    )

    if diagnostics:
        diagnostics.emit(
            "search.provider_select",
            "Selected provider for search",
            {
                "query": query,
                "num_results": num_results,
                "provider": selected.name,
                **{provider.diagnostics_key: ok for provider, ok in statuses},
            },
        )

    async def _run(client: httpx.AsyncClient) -> list[WebSearchResult]:
        return await provider_fn(query, num_results=num_results, http_client=client)

    # Converted here rather than in the MCP tool because this is the only place
    # the selected provider's label is known; deriving it a second time in the
    # tool would duplicate the selection and let the two disagree. It also means
    # the unconverted exception never travels further than this frame.
    try:
        if http_client is not None:
            return await _run(http_client)

        async with httpx.AsyncClient(timeout=30) as client:
            return await _run(client)
    except httpx.HTTPError as exc:
        raise _without_request_url(selected.label, exc) from exc
