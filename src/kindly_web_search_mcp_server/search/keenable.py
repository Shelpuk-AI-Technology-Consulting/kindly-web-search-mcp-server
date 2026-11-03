"""Keenable search provider.

Queries Keenable's web search API and maps its ``results`` onto
:class:`~kindly_web_search_mcp_server.models.WebSearchResult`. It is the last entry
in :data:`~kindly_web_search_mcp_server.search.PROVIDERS`, so it serves a query only
when ``KEENABLE_API_KEY`` is the one provider variable configured.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult

SEARCH_ENDPOINT = "https://api.keenable.ai/v1/search"

#: Keenable's ``snippet`` can be a long page excerpt; ``description`` is preferred
#: and the excerpt is only used, truncated, when no description is present.
SNIPPET_MAX_CHARS = 500


class KeenableError(RuntimeError):
    """Report a Keenable response this provider cannot turn into results."""


class KeenableConfigError(KeenableError):
    """Report that Keenable was called without a usable ``KEENABLE_API_KEY``."""


def _get_keenable_api_key() -> str:
    """Read the Keenable API key from the environment.

    Returns:
        The key, with surrounding whitespace removed.

    Raises:
        KeenableConfigError: If ``KEENABLE_API_KEY`` is unset, empty, or only whitespace.
    """
    api_key = os.environ.get("KEENABLE_API_KEY", "").strip()
    if not api_key:
        raise KeenableConfigError(
            "KEENABLE_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_keenable(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Keenable and return parsed organic results.

    Keenable endpoint:

    - ``POST https://api.keenable.ai/v1/search`` with JSON ``{"query", "max_results"}``
    - Headers: ``X-API-Key`` and ``X-Keenable-Title``

    Docs: https://docs.keenable.ai

    Args:
        query: The search query. A blank query returns no results without a request.
        num_results: Maximum number of results to return. A value below 1 returns no
            results without a request.
        http_client: Client to send the request with. A short-lived client with a
            30-second timeout is created when omitted.

    Returns:
        At most ``num_results`` results, in the order Keenable ranked them, each with
        an empty ``page_content``.

    Raises:
        KeenableConfigError: If ``KEENABLE_API_KEY`` is not usable.
        KeenableError: If the response is not a JSON object, has no ``results`` list,
            or holds results none of which could be parsed.
        httpx.HTTPError: If the request fails or Keenable answers with an error
            status. The router converts it so the request URL is not quoted.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_keenable_api_key()
    payload = {"query": query, "max_results": int(num_results)}
    headers = {
        "X-API-Key": api_key,
        "X-Keenable-Title": "kindly-web-search-mcp-server",
        "Content-Type": "application/json",
    }

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        """Send the search request and decode its JSON object.

        Args:
            client: The client to send the request with.

        Returns:
            The decoded response body.

        Raises:
            KeenableError: If the body is not valid JSON or not a JSON object.
            httpx.HTTPError: If the request fails or Keenable answers with an
                error status.
        """
        resp = await client.post(SEARCH_ENDPOINT, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise KeenableError("Keenable response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise KeenableError("Keenable response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw = data.get("results")
    if not isinstance(raw, list):
        raise KeenableError("Keenable response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
        if not isinstance(title, str) or not isinstance(link, str):
            continue

        # Prefer `description`; fall back to a truncated `snippet`. A result with
        # neither is still a usable link, since `page_content` is fetched later.
        description = item.get("description")
        excerpt = item.get("snippet")
        if isinstance(description, str) and description.strip():
            snippet = description
        elif isinstance(excerpt, str):
            snippet = excerpt[:SNIPPET_MAX_CHARS]
        else:
            snippet = ""

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    if raw and not results:
        raise KeenableError(
            f"Keenable returned {len(raw)} result(s) but none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have changed."
        )

    return results
