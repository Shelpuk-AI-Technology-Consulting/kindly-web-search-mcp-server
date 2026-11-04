"""Firecrawl search provider.

Queries Firecrawl's v2 search endpoint and maps its ``data.web`` results onto
:class:`~kindly_web_search_mcp_server.models.WebSearchResult`. Only search is
requested: no ``scrapeOptions`` are sent, so ``page_content`` is still filled by
this server's own resolver chain, as it is for every other provider. It is the
last entry in :data:`~kindly_web_search_mcp_server.search.PROVIDERS`, so it
serves a query only when ``FIRECRAWL_API_KEY`` is the one provider variable
configured.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult

SEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search"

#: Sent as ``origin`` in the request body so Firecrawl can tell requests from this
#: server apart from other integrations.
ORIGIN = "kindly-web-search-mcp-server"

#: The API documents ``limit`` as 1-100 and answers a larger value with HTTP 400.
MAX_LIMIT = 100


class FirecrawlError(RuntimeError):
    """Report a Firecrawl response this provider cannot turn into results."""


class FirecrawlConfigError(FirecrawlError):
    """Report that Firecrawl was called without a usable ``FIRECRAWL_API_KEY``."""


def _get_firecrawl_api_key() -> str:
    """Read the Firecrawl API key from the environment.

    Returns:
        The key, with surrounding whitespace removed.

    Raises:
        FirecrawlConfigError: If ``FIRECRAWL_API_KEY`` is unset, empty, or only
            whitespace.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise FirecrawlConfigError(
            "FIRECRAWL_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


async def search_firecrawl(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Firecrawl search and return parsed web results.

    Firecrawl endpoint:

    - ``POST https://api.firecrawl.dev/v2/search``
    - Header: ``Authorization: Bearer <FIRECRAWL_API_KEY>``
    - JSON body: ``{"query": "<query>", "limit": <num_results>, "origin": "kindly-web-search-mcp-server"}``

    ``limit`` is clamped to :data:`MAX_LIMIT`, because the API rejects a larger
    value outright, which would turn a large ``num_results`` into a failed search
    instead of a smaller one. ``youcom.py`` and ``sofya.py`` clamp for the same
    reason. The returned list is still capped at ``num_results`` here.

    Docs: https://docs.firecrawl.dev/api-reference/endpoint/search?utm_source=kindly-web-search-mcp-server&utm_medium=integration

    Args:
        query: The search query. A blank query returns no results without a
            request.
        num_results: Maximum number of results to return. A value below 1
            returns no results without a request.
        http_client: Client to send the request with. A short-lived client with a
            30-second timeout is created when omitted.

    Returns:
        At most ``num_results`` results, in the order Firecrawl ranked them, each
        with an empty ``page_content``.

    Raises:
        FirecrawlConfigError: If ``FIRECRAWL_API_KEY`` is not usable.
        FirecrawlError: If the response is not a JSON object, carries no ``data``
            object, carries a ``data.web`` that is not a list, or holds results
            none of which could be parsed. An *absent* ``web`` key is not an
            error -- it returns no results.
        httpx.HTTPError: If the request fails or Firecrawl answers with an error
            status. The router converts it so the request URL is not quoted.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_firecrawl_api_key()
    payload = {"query": query, "limit": min(int(num_results), MAX_LIMIT), "origin": ORIGIN}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        """Send the search request and decode its JSON object.

        Args:
            client: The client to send the request with.

        Returns:
            The decoded response body.

        Raises:
            FirecrawlError: If the body is not valid JSON or not a JSON object.
            httpx.HTTPError: If the request fails or Firecrawl answers with an
                error status.
        """
        resp = await client.post(SEARCH_ENDPOINT, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise FirecrawlError("Firecrawl response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise FirecrawlError("Firecrawl response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    # A reshaped container raises rather than returning `[]`, so a schema change
    # cannot pass for a query with no hits.
    container = data.get("data")
    if not isinstance(container, dict):
        raise FirecrawlError("Firecrawl response missing `data` object.")

    # An absent `web` key is read as "nothing matched", the way the rule file
    # treats empty results. A `web` key of the wrong type is a reshaped envelope.
    if "web" not in container:
        return []

    raw = container.get("web")
    if not isinstance(raw, list):
        raise FirecrawlError("Firecrawl response `data.web` is not a list.")

    results: list[WebSearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
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
        raise FirecrawlError(
            f"Firecrawl returned {len(raw)} result(s) but none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have changed."
        )

    return results
