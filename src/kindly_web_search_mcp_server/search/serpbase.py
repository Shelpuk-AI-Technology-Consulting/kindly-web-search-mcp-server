from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult

#: The one endpoint this provider talks to. A module constant rather than a
#: local, so ``tests/test_serpbase_live.py`` sends the request this code sends
#: instead of a copy of it -- a live test built on a duplicated URL stops
#: checking the provider the moment the two drift. `apifare.py` names its
#: endpoint the same way and for the same reason.
SEARCH_ENDPOINT = "https://api.serpbase.dev/google/search"


class SerpbaseError(RuntimeError):
    pass


class SerpbaseConfigError(SerpbaseError):
    pass


def _get_serpbase_api_key() -> str:
    api_key = os.environ.get("SERPBASE_API_KEY", "").strip()
    if not api_key:
        raise SerpbaseConfigError(
            "SERPBASE_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_serpbase(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query SerpBase and return parsed Google organic results.

    SerpBase endpoint:
    - POST https://api.serpbase.dev/google/search
    - Header: X-API-Key
    - JSON: {"q": "<query>"}
    - Returns JSON with ``organic`` array (title, link, snippet, position).

    Docs: https://serpbase.dev/docs
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_serpbase_api_key()
    payload = {"q": query}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(SEARCH_ENDPOINT, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise SerpbaseError("SerpBase response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise SerpbaseError("SerpBase response was not a JSON object.")
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

        # ``page_content`` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    return results
