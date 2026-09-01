from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult

# The API documents `count` as the per-section maximum, 1-100.
MAX_COUNT = 100

# The sections this module knows how to parse. Named once because the parser and
# the schema-mismatch guard must agree on which sections they are: counting a
# section the parser never reads turns a legitimate zero-hit answer into an error.
PARSED_SECTIONS = ("web", "news")


class YoucomError(RuntimeError):
    pass


class YoucomConfigError(YoucomError):
    pass


def _get_youcom_api_key() -> str:
    api_key = os.environ.get("YDC_API_KEY", "").strip()
    if not api_key:
        raise YoucomConfigError(
            "YDC_API_KEY is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return api_key


def _collect_results(data: dict[str, Any], num_results: int) -> list[WebSearchResult] | None:
    """Extract usable results from a You.com search response.

    The API returns ``results.web`` and, when the query has news intent,
    ``results.news``, as separate lists of differently-shaped objects. Both carry
    the three fields this server needs — ``title``, ``url`` and a snippet
    (``description``, with ``snippets[0]`` as a longer alternative) — so both are
    folded into one ranked-by-arrival list and capped at ``num_results``.

    Args:
        data: Parsed JSON response body.
        num_results: Maximum number of results to return.

    Returns:
        Usable results, at most ``num_results`` of them, or ``None`` when the
        response does not contain a ``results`` object at all.
    """
    raw = data.get("results")
    if not isinstance(raw, dict):
        return None

    results: list[WebSearchResult] = []
    for section in PARSED_SECTIONS:
        entries = raw.get(section)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            link = item.get("url")
            if not isinstance(title, str) or not isinstance(link, str):
                continue

            # `description` is the SERP snippet; `snippets[0]` carries a longer
            # excerpt when the API returns one. Take the first that is actually a
            # non-empty string rather than trusting the shape.
            snippets = item.get("snippets")
            longest = snippets[0] if isinstance(snippets, list) and snippets else None
            candidates = (item.get("description"), longest)
            snippet = next(
                (value for value in candidates if isinstance(value, str) and value), ""
            )

            # `page_content` is populated later by the MCP tool (best-effort).
            results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
            if len(results) >= num_results:
                return results

    return results


async def search_youcom(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Query You.com Search API and return parsed results.

    You.com endpoint:
    - POST https://ydc-index.io/v1/search
    - Header: X-API-Key
    - JSON: {"query": "<query>", "count": <num_results>}

    `count` is documented as the per-section maximum (default 10, max 100), and
    the response can carry both a `web` and a `news` section, so the caller's
    `num_results` is applied locally after merging the sections rather than
    trusted as a global bound.

    Docs: https://you.com/docs/guides/search
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    api_key = _get_youcom_api_key()
    url = "https://ydc-index.io/v1/search"
    # Clamped rather than passed through: the API documents 1-100 and rejects
    # anything outside it, which would turn a large `num_results` into a failed
    # search instead of a smaller one. `sofya.py` clamps for the same reason.
    payload = {"query": query, "count": min(int(num_results), MAX_COUNT)}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise YoucomError("You.com response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise YoucomError("You.com response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    results = _collect_results(data, num_results)
    if results is None:
        raise YoucomError("You.com response missing `results` object.")

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    # Re-read rather than asserted: `_collect_results` has already verified the
    # shape, and an `assert` here would be stripped under `python -O`, turning a
    # handled case into an AttributeError.
    raw = data.get("results")
    parsed_entries = sum(
        len(entries)
        for section, entries in (raw.items() if isinstance(raw, dict) else ())
        if section in PARSED_SECTIONS and isinstance(entries, list)
    )
    if parsed_entries and not results:
        raise YoucomError(
            f"You.com returned {parsed_entries} result(s) in {PARSED_SECTIONS} but "
            "none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have changed."
        )

    return results
