from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


class SofyaError(RuntimeError):
    pass


class SofyaConfigError(SofyaError):
    pass


def _get_sofya_api_key() -> str:
    api_key = os.environ.get("SOFYA_API_KEY", "").strip()
    if not api_key:
        raise SofyaConfigError(
            "SOFYA_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_sofya(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Query Sofya Search API and return parsed results.

    Sofya endpoint:
    - POST https://sofya.co/v1/search
    - Header: Authorization: Bearer <SOFYA_API_KEY>
    - JSON: {"query": "<query>", "max_results": <num_results>, "search_depth": "snippets"}

    Docs: https://sofya.co/docs
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_sofya_api_key()
    url = "https://sofya.co/v1/search"
    payload = {
        "query": query,
        "max_results": int(num_results),
        "search_depth": "snippets",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise SofyaError("Sofya response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise SofyaError("Sofya response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        raise SofyaError("Sofya response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        link = item.get("url")
        snippet = item.get("content")
        if not isinstance(title, str) or not isinstance(link, str) or not isinstance(snippet, str):
            continue

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    return results
