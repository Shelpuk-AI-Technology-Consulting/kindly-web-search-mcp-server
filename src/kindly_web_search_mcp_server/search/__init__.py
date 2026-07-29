"""Search providers (Serper → SerpBase → Tavily → SearXNG → Sofya).

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
from .sofya import search_sofya
from .tavily import search_tavily


# The provider coroutines are re-exported deliberately. `SearchProviderSpec` resolves
# them by attribute name at call time rather than holding a reference, so a static
# reader (and the linter) sees no use for the imports above -- but rebinding these
# module attributes is exactly how tests substitute providers.
__all__ = [
    "PROVIDERS",
    "SearchProviderSpec",
    "WebSearchProviderError",
    "any_provider_configured",
    "provider_env_vars",
    "search_searxng",
    "search_serpbase",
    "search_serper",
    "search_sofya",
    "search_tavily",
    "search_web",
]


class WebSearchProviderError(RuntimeError):
    pass


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

    if http_client is not None:
        return await _run(http_client)

    async with httpx.AsyncClient(timeout=30) as client:
        return await _run(client)
