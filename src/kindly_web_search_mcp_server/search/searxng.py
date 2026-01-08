from __future__ import annotations

import json
import os
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from ..models import WebSearchResult


class SearxngError(RuntimeError):
    pass


class SearxngConfigError(SearxngError):
    pass


LOGGER = logging.getLogger(__name__)

DEFAULT_SEARXNG_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _get_searxng_base_url() -> str:
    base_url = os.environ.get("SEARXNG_BASE_URL", "").strip()
    if not base_url:
        raise SearxngConfigError(
            "SEARXNG_BASE_URL is not set. Configure it as an environment variable in your IDE/run configuration."
        )

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise SearxngConfigError(f"SEARXNG_BASE_URL is not a valid URL: {base_url!r}")

    return base_url.rstrip("/")


def _build_headers() -> dict[str, str]:
    headers: dict[str, str] = {}

    raw_extra = (os.environ.get("SEARXNG_HEADERS_JSON") or "").strip()
    if raw_extra:
        try:
            parsed = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            raise SearxngConfigError("SEARXNG_HEADERS_JSON must be a JSON object string.") from exc

        if not isinstance(parsed, dict):
            raise SearxngConfigError("SEARXNG_HEADERS_JSON must be a JSON object string.")

        for key, value in parsed.items():
            if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                headers[key] = value

    if "user-agent" not in {key.lower() for key in headers.keys()}:
        headers["User-Agent"] = os.environ.get("SEARXNG_USER_AGENT", "").strip() or DEFAULT_SEARXNG_USER_AGENT

    return headers


def _get_request_timeout_seconds() -> float | None:
    raw = (os.environ.get("SEARXNG_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise SearxngConfigError("SEARXNG_TIMEOUT_SECONDS must be a number (seconds).") from exc


def _looks_like_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def search_searxng(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """
    Query a SearXNG instance and return parsed results.

    SearXNG endpoint:
    - GET {SEARXNG_BASE_URL}/search
    - Params: q=<query>, format=json, plus optional params like language/categories/engines/time_range/safesearch.

    SearXNG docs: https://docs.searxng.org/dev/search_api.html
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    base_url = _get_searxng_base_url()
    url = f"{base_url}/search"

    params: dict[str, Any] = {"q": query, "format": "json"}
    for env_key, param_key in (
        ("SEARXNG_LANGUAGE", "language"),
        ("SEARXNG_CATEGORIES", "categories"),
        ("SEARXNG_ENGINES", "engines"),
        ("SEARXNG_TIME_RANGE", "time_range"),
        ("SEARXNG_SAFESEARCH", "safesearch"),
    ):
        value = (os.environ.get(env_key) or "").strip()
        if value:
            params[param_key] = value

    headers = _build_headers()
    timeout_seconds = _get_request_timeout_seconds()

    async def _do_request(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.get(url, params=params, headers=headers, timeout=timeout_seconds)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                raise SearxngError(
                    "SearXNG returned 403 Forbidden. JSON output may be disabled on the instance "
                    "(formats are configured in settings.yml; request uses format=json). "
                    "Fix: enable the 'json' format in the SearXNG instance configuration."
                ) from exc
            if status == 429:
                raise SearxngError("SearXNG returned 429 Too Many Requests (rate limited).") from exc
            raise SearxngError(f"SearXNG returned HTTP {status}.") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise SearxngError("SearXNG response was not valid JSON.") from exc

        if not isinstance(data, dict):
            raise SearxngError("SearXNG response was not a JSON object.")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _do_request(client)
    else:
        data = await _do_request(http_client)

    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        raise SearxngError("SearXNG response missing `results` list.")

    if not raw_results:
        LOGGER.debug("SearXNG returned empty results list for query=%r", query)

    results: list[WebSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
        snippet = item.get("content")

        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(link, str) or not link.strip() or not _looks_like_url(link):
            continue
        if not isinstance(snippet, str) or not snippet.strip():
            continue

        results.append(WebSearchResult(title=title, link=link, snippet=snippet, page_content=""))
        if len(results) >= num_results:
            break

    return results
