from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


class SerperError(RuntimeError):
    pass


def _get_serper_api_key() -> str:
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        raise SerperError(
            "SERPER_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_serper(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Serper and return parsed organic results.

    Serper endpoint:
    - POST https://google.serper.dev/search
    - Header: X-API-KEY
    - JSON: {"q": "<query>", "num": <num_results>}
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_serper_api_key()
    url = "https://google.serper.dev/search"
    payload = {"q": query, "num": int(num_results)}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise SerperError("Serper response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    organic = data.get("organic", [])
    if not isinstance(organic, list):
        return []

    results: list[WebSearchResult] = []
    for item in organic:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("link")
        snippet = item.get("snippet")
        if not isinstance(title, str) or not isinstance(link, str) or not isinstance(snippet, str):
            continue

        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=None))
        if len(results) >= num_results:
            break

    return results

