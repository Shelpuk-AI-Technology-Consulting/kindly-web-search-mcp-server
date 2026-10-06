"""Cohesivity search provider.

Queries Cohesivity search and maps its ``results`` onto
:class:`~kindly_web_search_mcp_server.models.WebSearchResult`. It is the last
entry in :data:`~kindly_web_search_mcp_server.search.PROVIDERS`, so it serves a
query only when ``COHESIVITY_APPLICATION_KEY`` is the one provider variable
configured.

**The application key travels in the request URL**, as the ``key`` query
parameter, because that is the only form the service accepts: it rejects the key
in an ``Authorization`` header, an ``X-Api-Key`` header and the request body.
That makes every value derived from the request URL credential-bearing. The key
is handed to ``httpx`` through ``params=`` and nowhere else -- never formatted
into a message, a log line or a diagnostics payload -- and an ``httpx`` failure,
whose message quotes the full URL, is left to reach the router, which rebuilds
it without the URL. Catching that failure here and re-quoting it would put the
key in front of the MCP client.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult

#: The one endpoint this provider talks to. The path is fixed by the service. A
#: module constant rather than a local, so a test compares against the request
#: this code sends rather than a copy of it.
SEARCH_ENDPOINT = "https://cohesivity.ai/edge/exa-api/search"

#: Search mode sent with every request. ``auto`` is one of the modes an
#: anonymous tenant is allowed; others answer HTTP 403.
SEARCH_TYPE = "auto"

#: Sentences per highlight. Without a ``contents`` request the service returns
#: only an id, a title and a URL, so highlights are what supply the snippet.
HIGHLIGHT_SENTENCES = 2

#: Longest snippet kept. Highlights are extracted page text rather than a
#: search-engine snippet, so they are bounded here instead of trusting the
#: service to keep them short.
SNIPPET_MAX_CHARS = 500


class CohesivityError(RuntimeError):
    """Report a Cohesivity response this provider cannot turn into results."""


class CohesivityConfigError(CohesivityError):
    """Report that Cohesivity was called without a usable ``COHESIVITY_APPLICATION_KEY``."""


def _get_cohesivity_application_key() -> str:
    """Read the Cohesivity application key from the environment.

    Returns:
        The key, with surrounding whitespace removed.

    Raises:
        CohesivityConfigError: If ``COHESIVITY_APPLICATION_KEY`` is unset, empty,
            or only whitespace.
    """
    key = os.environ.get("COHESIVITY_APPLICATION_KEY", "").strip()
    if not key:
        raise CohesivityConfigError(
            "COHESIVITY_APPLICATION_KEY is not set. Configure it as an environment "
            "variable in your IDE/run configuration."
        )
    return key


def _snippet_from(item: dict[str, Any]) -> str:
    """Build a snippet from a result's highlights.

    Highlights are joined with single spaces, runs of whitespace (page text
    often carries newlines and indentation) are collapsed, and the result is
    capped at :data:`SNIPPET_MAX_CHARS`. Entries that are not strings are
    skipped rather than failing the result.

    Args:
        item: One entry of the response's ``results`` list.

    Returns:
        The snippet, or ``""`` when the result carries no usable highlight.
    """
    highlights = item.get("highlights")
    if not isinstance(highlights, list):
        return ""

    snippet = " ".join(
        " ".join(text.split()) for text in highlights if isinstance(text, str)
    ).strip()
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[: SNIPPET_MAX_CHARS - 1].rstrip() + "…"
    return snippet


async def search_cohesivity(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Cohesivity search and return parsed results.

    Cohesivity endpoint:

    - ``POST`` :data:`SEARCH_ENDPOINT`
    - Query parameter: ``key=<COHESIVITY_APPLICATION_KEY>``
    - JSON body: ``{"query": "<query>", "numResults": <num_results>,
      "type": "auto", "contents": {"highlights": {"numSentences": 2}}}``

    ``numResults`` is forwarded as given; the returned list is capped at
    ``num_results`` here instead. A result without a string ``title`` and
    ``url`` is dropped; one without highlights is kept with an empty snippet.

    Docs: https://cohesivity.ai

    Args:
        query: The search query. A blank query returns no results without a
            request.
        num_results: Maximum number of results to return. A value below 1
            returns no results without a request.
        http_client: Client to send the request with. A short-lived client with
            a 30-second timeout is created when omitted.

    Returns:
        At most ``num_results`` results, in the order Cohesivity ranked them,
        each with an empty ``page_content``.

    Raises:
        CohesivityConfigError: If ``COHESIVITY_APPLICATION_KEY`` is not usable.
        CohesivityError: If the response is not a JSON object, has no
            ``results`` list, or holds results none of which could be parsed.
        httpx.HTTPError: If the request fails or Cohesivity answers with an
            error status -- 401 for a rejected key, 403 when search is not
            provisioned for the tenant, 429 when rate-limited. Its message
            quotes the request URL, and so the key; the router converts it so
            neither reaches the MCP client.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    key = _get_cohesivity_application_key()
    payload = {
        "query": query,
        "numResults": int(num_results),
        "type": SEARCH_TYPE,
        "contents": {"highlights": {"numSentences": HIGHLIGHT_SENTENCES}},
    }

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        """Send the search request and decode its JSON object.

        Args:
            client: The client to send the request with.

        Returns:
            The decoded response body.

        Raises:
            CohesivityError: If the body is not valid JSON or not a JSON object.
            httpx.HTTPError: If the request fails or Cohesivity answers with an
                error status.
        """
        # `params=` is the only place the key goes; see the module docstring.
        resp = await client.post(SEARCH_ENDPOINT, params={"key": key}, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise CohesivityError("Cohesivity response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise CohesivityError("Cohesivity response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw = data.get("results")
    if not isinstance(raw, list):
        raise CohesivityError("Cohesivity response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
        if not isinstance(title, str) or not isinstance(link, str):
            continue

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(
            WebSearchResult(
                title=title, link=link, snippet=_snippet_from(item), page_content=""
            )
        )
        if len(results) >= num_results:
            break

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    if raw and not results:
        raise CohesivityError(
            f"Cohesivity returned {len(raw)} result(s) but none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have "
            "changed."
        )

    return results
