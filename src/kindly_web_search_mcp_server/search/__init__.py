"""Search providers (Serper → Tavily → SearXNG)."""
from __future__ import annotations

import os
from typing import Awaitable, Callable

import httpx

from ..models import WebSearchResult
from .searxng import search_searxng
from .serper import search_serper
from .tavily import search_tavily


class WebSearchProviderError(RuntimeError):
    pass


def _has_serper_key() -> bool:
    return bool(os.environ.get("SERPER_API_KEY", "").strip())


def _has_tavily_key() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY", "").strip())


def _has_searxng_config() -> bool:
    return bool(os.environ.get("SEARXNG_BASE_URL", "").strip())


async def search_web(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Search the web using Serper, Tavily, or SearXNG.

    Selection (strict order, no cross-provider fallback):
    - If SERPER_API_KEY is set: use Serper.
    - Else if TAVILY_API_KEY is set: use Tavily.
    - Else if SEARXNG_BASE_URL is set: use SearXNG.
    """
    has_serper = _has_serper_key()
    has_tavily = _has_tavily_key()
    has_searxng = _has_searxng_config()
    if not has_serper and not has_tavily and not has_searxng:
        raise WebSearchProviderError(
            "No web search provider is configured. Set SERPER_API_KEY, TAVILY_API_KEY, or SEARXNG_BASE_URL."
        )

    provider: Callable[..., Awaitable[list[WebSearchResult]]]
    if has_serper:
        provider = search_serper
    elif has_tavily:
        provider = search_tavily
    else:
        provider = search_searxng

    async def _run(client: httpx.AsyncClient) -> list[WebSearchResult]:
        return await provider(query, num_results=num_results, http_client=client)

    if http_client is not None:
        return await _run(http_client)

    async with httpx.AsyncClient(timeout=30) as client:
        return await _run(client)

