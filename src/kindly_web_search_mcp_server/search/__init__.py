"""Search providers (Serper first)."""
from __future__ import annotations

import os
from typing import Awaitable, Callable

import httpx

from ..models import WebSearchResult
from .serper import SerperConfigError, SerperError, search_serper
from .tavily import TavilyConfigError, TavilyError, search_tavily


class WebSearchProviderError(RuntimeError):
    pass


def _has_serper_key() -> bool:
    return bool(os.environ.get("SERPER_API_KEY", "").strip())


def _has_tavily_key() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY", "").strip())


def _should_fallback(exc: BaseException) -> bool:
    # Explicitly do not fallback on auth/config issues.
    if isinstance(exc, (SerperConfigError, TavilyConfigError)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403, 400):
            return False
        return status >= 500 or status == 429
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    return False


async def search_web(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Search the web using Serper and/or Tavily.

    Selection:
    - If both keys are set: use Serper, fallback to Tavily on transient failure.
    - If only one key is set: use that provider.
    """
    has_serper = _has_serper_key()
    has_tavily = _has_tavily_key()
    if not has_serper and not has_tavily:
        raise WebSearchProviderError("Neither SERPER_API_KEY nor TAVILY_API_KEY is set.")

    primary: Callable[..., Awaitable[list[WebSearchResult]]]
    secondary: Callable[..., Awaitable[list[WebSearchResult]]] | None = None

    if has_serper:
        primary = search_serper
        secondary = search_tavily if has_tavily else None
    else:
        primary = search_tavily

    async def _run(provider: Callable[..., Awaitable[list[WebSearchResult]]], client: httpx.AsyncClient):
        return await provider(query, num_results=num_results, http_client=client)

    if http_client is not None:
        try:
            return await _run(primary, http_client)
        except Exception as exc:
            if secondary is not None and _should_fallback(exc):
                return await _run(secondary, http_client)
            raise

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            return await _run(primary, client)
        except Exception as exc:
            if secondary is not None and _should_fallback(exc):
                try:
                    return await _run(secondary, client)
                except Exception as secondary_exc:
                    raise WebSearchProviderError(
                        "Primary web search failed "
                        f"({type(exc).__name__}); fallback also failed ({type(secondary_exc).__name__})."
                    ) from exc
            raise
