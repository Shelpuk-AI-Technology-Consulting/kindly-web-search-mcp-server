"""Serply search provider.

Queries Serply's Google web search endpoint and maps its organic ``results`` onto
:class:`~kindly_web_search_mcp_server.models.WebSearchResult`. It is the last entry
in :data:`~kindly_web_search_mcp_server.search.PROVIDERS`, so it serves a query only
when ``SERPLY_API_KEY`` is the one provider variable configured.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import httpx

from ..models import WebSearchResult

# The URL-encoded parameters are appended to this path rather than sent as a query
# string: the path form is the only one Serply's reference and example code show.
SEARCH_ENDPOINT = "https://api.serply.io/v1/search/"


class SerplyError(RuntimeError):
    """Report a Serply response this provider cannot turn into results."""


class SerplyConfigError(SerplyError):
    """Report that Serply was called without a usable ``SERPLY_API_KEY``."""


def _get_serply_api_key() -> str:
    """Read the Serply API key from the environment.

    Returns:
        The key, with surrounding whitespace removed.

    Raises:
        SerplyConfigError: If ``SERPLY_API_KEY`` is unset, empty, or only whitespace.
    """
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

    - ``GET https://api.serply.io/v1/search/q=<query>&num=<num_results>``, with the
      parameters URL-encoded into the path
    - Header: ``X-Api-Key``

    ``num`` is forwarded as given because Serply documents no maximum; the returned
    list is capped at ``num_results`` here instead.

    Docs: https://serply.io/docs/resources/google-search

    Args:
        query: The search query. A blank query returns no results without a request.
        num_results: Maximum number of results to return. A value below 1 returns no
            results without a request.
        http_client: Client to send the request with. A short-lived client with a
            30-second timeout is created when omitted.

    Returns:
        At most ``num_results`` results, in the order Serply ranked them, each with
        an empty ``page_content``.

    Raises:
        SerplyConfigError: If ``SERPLY_API_KEY`` is not usable.
        SerplyError: If the response is not a JSON object, has no ``results`` list,
            or holds results none of which could be parsed.
        httpx.HTTPError: If the request fails or Serply answers with an error
            status. The router converts it so the request URL is not quoted.
        httpx.InvalidURL: If the query makes the URL longer than httpx accepts.
            It is not an :class:`httpx.HTTPError`, so the router passes it
            through; its message carries neither the URL nor the key.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_serply_api_key()
    # URL-encoding stops `&`, `+` and `/` in the query from splitting the path's parameters.
    url = SEARCH_ENDPOINT + urlencode({"q": query, "num": int(num_results)})
    headers = {"X-Api-Key": api_key}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        """Send the search request and decode its JSON object.

        Args:
            client: The client to send the request with.

        Returns:
            The decoded response body.

        Raises:
            SerplyError: If the body is not valid JSON or not a JSON object.
            httpx.HTTPError: If the request fails or Serply answers with an
                error status.
        """
        resp = await client.get(url, headers=headers)
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
