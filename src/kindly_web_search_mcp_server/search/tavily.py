from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


class TavilyError(RuntimeError):
    pass


class TavilyConfigError(TavilyError):
    pass


def _get_tavily_api_key() -> str:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise TavilyConfigError(
            "TAVILY_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_tavily(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Query Tavily Search API and return parsed results.

    Tavily endpoint:
    - POST https://api.tavily.com/search
    - Header: Authorization: Bearer <TAVILY_API_KEY>
    - JSON: {"query": "<query>", "max_results": <num_results>, "search_depth": "basic", ...}

    Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_tavily_api_key()
    url = "https://api.tavily.com/search"
    payload = {
        "query": query,
        "max_results": int(num_results),
        "search_depth": "basic",
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise TavilyError("Tavily response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise TavilyError("Tavily response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        raise TavilyError("Tavily response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        link = item.get("url")
        snippet = item.get("content")
        if not isinstance(title, str) or not isinstance(link, str) or not isinstance(snippet, str):
            continue

        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=None))
        if len(results) >= num_results:
            break

    return results
