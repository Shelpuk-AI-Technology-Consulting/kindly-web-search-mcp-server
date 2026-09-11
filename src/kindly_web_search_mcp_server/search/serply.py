from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


class SerplyError(RuntimeError):
    pass


class SerplyConfigError(SerplyError):
    pass


def _get_serply_api_key() -> str:
    api_key = os.environ.get("SERPLY_API_KEY", "").strip()
    if not api_key:
        raise SerplyConfigError(
            "SERPLY_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_serply(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Serply and return parsed organic results.

    Serply endpoint:
    - GET https://api.serply.io/v1/search
    - Header: X-Api-Key
    - Query: q=<query>&num=<num_results>

    `num` is forwarded as given. The API answers an oversized value with fewer
    results rather than an error, so there is nothing to clamp.

    Docs: https://serply.io/docs
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_serply_api_key()
    url = "https://api.serply.io/v1/search"
    params: dict[str, str | int] = {"q": query, "num": int(num_results)}
    headers = {"X-Api-Key": api_key}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise SerplyError("Serply response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise SerplyError("Serply response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw = data.get("results")
    if not isinstance(raw, list):
        raise SerplyError("Serply response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("link")
        if not isinstance(title, str) or not isinstance(link, str):
            continue

        # `description` is the SERP snippet. A result without one is still a
        # usable link, since `page_content` is fetched later.
        description = item.get("description")
        snippet = description if isinstance(description, str) else ""

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    if raw and not results:
        raise SerplyError(
            f"Serply returned {len(raw)} result(s) but none could be parsed; "
            "each needs a string `title` and `link`. The response schema may have changed."
        )

    return results
