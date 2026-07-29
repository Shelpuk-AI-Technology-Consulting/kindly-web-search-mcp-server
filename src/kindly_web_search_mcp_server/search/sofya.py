from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


# The API documents `max_results` as accepting 1-20.
MAX_RESULTS = 20


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
    - JSON: {"query": "<query>", "max_results": <clamped to 1..20>, "search_depth": "snippets"}

    `snippets` costs 1 credit and returns SERP text without fetching pages, while
    `basic` costs 3 and returns extracted page content. The cheap depth is correct
    here because this server resolves `page_content` itself with its own browser,
    so paying Sofya to fetch the same pages would be wasted.

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
        # The API documents max_results as 1-20 and rejects anything outside that
        # with a 400, so clamp rather than forward a caller's larger request.
        "max_results": max(1, min(int(num_results), MAX_RESULTS)),
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
        if not isinstance(title, str) or not isinstance(link, str):
            continue

        # `content` holds extracted page text and is populated only when Sofya
        # fetched the page. At `snippets` depth it does not, and the SERP text
        # arrives in `description` instead. Take the first field that actually
        # carries text: testing truthiness alone would latch onto a non-string
        # `content` and never reach `description`.
        snippet = next(
            (
                value
                for value in (item.get("content"), item.get("description"))
                if isinstance(value, str) and value
            ),
            "",
        )

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    if raw_results and not results:
        raise SofyaError(
            f"Sofya returned {len(raw_results)} result(s) but none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have changed."
        )

    return results
