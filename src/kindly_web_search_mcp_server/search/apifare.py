from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import WebSearchResult


class ApifareError(RuntimeError):
    pass


class ApifareConfigError(ApifareError):
    pass


class ApifarePaymentRequiredError(ApifareError):
    """Report an empty apifare balance together with the top-up link.

    apifare answers an exhausted balance with HTTP 402 and a structured body
    carrying a ``topup_url``. That URL is the actionable half of the error for
    the agent's operator — it contains only the account's public reference,
    never the bearer token — so it is surfaced in the message rather than
    collapsed into a bare status code.
    """


def _get_apifare_token() -> str:
    token = os.environ.get("APIFARE_TOKEN", "").strip()
    if not token:
        raise ApifareConfigError(
            "APIFARE_TOKEN is not set. Configure it as an environment variable in your IDE/run configuration."
        )
    return token


async def search_apifare(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query apifare's metered search and return parsed organic results.

    apifare endpoint:
    - POST https://apifare.com/v1/call/dataforseo
    - Header: Authorization: Bearer <APIFARE_TOKEN>
    - JSON: {"q": "<query>", "count": <num_results>}

    The call is debited from the account's prepaid balance at the listed
    per-call price. An exhausted balance returns HTTP 402 with a top-up URL,
    raised here as :class:`ApifarePaymentRequiredError` so the agent can relay
    the link instead of a bare failure.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    token = _get_apifare_token()
    url = "https://apifare.com/v1/call/dataforseo"
    payload = {"q": query, "count": int(num_results)}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 402:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            topup = body.get("topup_url") if isinstance(body, dict) else None
            message = "The apifare balance is empty."
            if isinstance(topup, str) and topup:
                message += f" Top up and retry: {topup}"
            raise ApifarePaymentRequiredError(message)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApifareError("apifare response was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ApifareError("apifare response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    result = data.get("result")
    items = result.get("results", []) if isinstance(result, dict) else []
    if not isinstance(items, list):
        return []

    results: list[WebSearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
        snippet = item.get("description")
        if not isinstance(title, str) or not isinstance(link, str) or not isinstance(snippet, str):
            continue

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    return results
