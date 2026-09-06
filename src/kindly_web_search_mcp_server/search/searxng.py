from __future__ import annotations

import json
import os
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from ..models import WebSearchResult
from ..utils.diagnostics import redact_url_credentials


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


def _get_searxng_base_urls() -> list[str]:
    """Resolve the configured SearXNG instances, in the order they will be tried

    ``SEARXNG_BASE_URL`` holds one URL or a comma-separated list. Entries that do
    not parse to both a scheme and a host are skipped rather than failing the
    whole list, so one bad entry does not disable a working instance beside it.

    Neither the rejected entry nor the error names a value, because this variable
    carries its credential in the URL's userinfo and a mistyped scheme is the
    ordinary way to reach both paths. See ``.system_design/TEST_SUITE.md``
    section 14.

    Returns:
        The configured base URLs with any trailing slash removed, in
        configuration order.

    Raises:
        SearxngConfigError: If the variable is unset or blank, or if no entry
            parses to both a scheme and a host.
    """
    raw = os.environ.get("SEARXNG_BASE_URL", "").strip()
    if not raw:
        raise SearxngConfigError(
            "SEARXNG_BASE_URL is not set. Configure it as an environment variable in your IDE/run configuration."
        )

    urls: list[str] = []
    for index, part in enumerate(raw.split(","), start=1):
        part = part.strip()
        if not part:
            continue
        parsed = urlparse(part)
        if not parsed.scheme or not parsed.netloc:
            # Position, not value: the entry has no scheme, so the redaction helper cannot strip its userinfo.
            LOGGER.warning(
                "Ignoring entry %d of SEARXNG_BASE_URL: no scheme or host.", index
            )
            continue
        urls.append(part.rstrip("/"))

    if not urls:
        # The rejected value is not quoted back: it carries a credential in its userinfo.
        raise SearxngConfigError(
            "No valid URLs found in SEARXNG_BASE_URL. Each entry must include a "
            "scheme and a host, for example https://searx.example.org."
        )

    return urls


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
    """Query the configured SearXNG instances in order and return parsed results

    Instances are tried in configuration order and the first that answers wins.
    Every per-instance failure is caught and the last one is re-raised inside a
    single aggregate, chained so the arm that produced it stays inspectable.

    SearXNG endpoint:
    - GET {SEARXNG_BASE_URL}/search
    - Params: q=<query>, format=json, plus optional params like language/categories/engines/time_range/safesearch.

    SearXNG docs: https://docs.searxng.org/dev/search_api.html

    Args:
        query: The search query to run. A blank query returns no results
            without a request.
        num_results: Maximum number of results to return. Less than one returns
            no results without a request.
        http_client: Client to reuse for the request. A short-lived client is
            created when omitted.

    Returns:
        The parsed results, at most ``num_results`` of them.

    Raises:
        SearxngConfigError: If ``SEARXNG_BASE_URL`` is unset or holds no entry
            with both a scheme and a host, if ``SEARXNG_HEADERS_JSON`` is not a
            JSON object, or if ``SEARXNG_TIMEOUT_SECONDS`` is not a number.
        SearxngError: If every configured instance failed, or if the instance
            that answered returned a body this parser cannot use. Both messages
            are served to the MCP client, so neither quotes a request URL --
            ``.system_design/TEST_SUITE.md`` section 14 records why.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    base_urls = _get_searxng_base_urls()

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

    async def _do_request_for_url(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
        url = f"{base_url}/search"
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

    last_error = None
    data = None

    for base_url in base_urls:
        # Redacted, not dropped: a URL that got this far has a scheme, so the host stays readable.
        LOGGER.info(
            "Attempting SearXNG query on instance: %s", redact_url_credentials(base_url)
        )
        try:
            if http_client is None:
                async with httpx.AsyncClient(timeout=30) as client:
                    data = await _do_request_for_url(client, base_url)
            else:
                data = await _do_request_for_url(http_client, base_url)
            break
        except Exception as exc:
            # Both operands are redacted: the message is a derived value too.
            LOGGER.warning(
                "SearXNG query failed on %s: %s",
                redact_url_credentials(base_url),
                redact_url_credentials(str(exc)),
            )
            last_error = exc
            continue

    if data is None:
        # Chained, not merely quoted: without `from` the arm that produced the message reaches a caller as a bare substring.
        # Redacted like the log copy above: this is the same operand on the more
        # exposed surface, since FastMCP serves this message to the MCP client.
        raise SearxngError(
            "All configured SearXNG instances failed. Last error: "
            f"{redact_url_credentials(str(last_error))}"
        ) from last_error

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
