from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import anyio
import httpx

from ..scrape.extract import extract_content_as_markdown
from ..scrape.sanitize import sanitize_markdown


class WikipediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class WikipediaTarget:
    api_base_url: str  # e.g. https://en.wikipedia.org/w/api.php
    canonical_url: str  # e.g. https://en.wikipedia.org/wiki/Apple_Inc.
    host: str  # e.g. en.wikipedia.org
    title: str  # e.g. Apple_Inc.


_WIKI_PATH_RE = re.compile(r"^/wiki/(.+)$")

# Namespaces we do not treat as “article content” for this retriever (MVP).
_NON_ARTICLE_NAMESPACE_PREFIXES = (
    "Talk:",
    "User:",
    "Wikipedia:",
    "File:",
    "Template:",
    "Category:",
    "Help:",
    "Portal:",
    "Special:",
    "Draft:",
    "MediaWiki:",
    "Module:",
)


def _normalize_host(host: str) -> str:
    """
    Normalize mobile Wikipedia hosts to the canonical desktop host.

    Examples:
    - en.m.wikipedia.org -> en.wikipedia.org
    - m.wikipedia.org -> en.wikipedia.org
    """
    host = host.lower().strip()
    if host == "m.wikipedia.org":
        return "en.wikipedia.org"
    if host.endswith(".m.wikipedia.org"):
        return host.replace(".m.wikipedia.org", ".wikipedia.org")
    return host


def parse_wikipedia_url(url: str) -> WikipediaTarget:
    """
    Parse a Wikipedia article URL and derive API base + canonical URL.

    Supported:
    - https://<lang>.wikipedia.org/wiki/Title
    - https://<lang>.wikipedia.org/w/index.php?title=Title
    """
    parsed = urlparse(url)
    host_raw = parsed.hostname or ""
    if not host_raw:
        raise WikipediaError("URL has no hostname.")

    host = _normalize_host(host_raw)
    if not host.endswith(".wikipedia.org"):
        raise WikipediaError(f"Unsupported Wikipedia host: {host}")

    title: str | None = None
    path = parsed.path or ""

    m = _WIKI_PATH_RE.match(path)
    if m:
        title = m.group(1)
    elif path == "/w/index.php":
        q = parse_qs(parsed.query or "")
        t = q.get("title", [None])[0]
        if isinstance(t, str) and t.strip():
            title = t

    if not title:
        raise WikipediaError("URL is not a recognized Wikipedia article URL.")

    # Decode percent-encoding; keep underscores (Wikipedia canonical form).
    title = unquote(title)
    title = title.replace(" ", "_").strip()
    if not title:
        raise WikipediaError("Empty article title.")

    # Reject non-article namespaces (MVP).
    if any(title.startswith(prefix) for prefix in _NON_ARTICLE_NAMESPACE_PREFIXES):
        raise WikipediaError("Non-article Wikipedia namespace is out of scope.")

    api_base_url = f"https://{host}/w/api.php"
    canonical_url = f"https://{host}/wiki/{title}"
    return WikipediaTarget(api_base_url=api_base_url, canonical_url=canonical_url, host=host, title=title)


def _default_user_agent() -> str:
    return os.environ.get(
        "WIKIPEDIA_USER_AGENT",
        "kindly-web-search-mcp-server/0.1 (https://github.com/)",
    ).strip()


def _strip_wikipedia_html_noise(html: str) -> str:
    """
    Best-effort cleanup of common Wikipedia HTML noise before HTML→Markdown conversion.

    We keep this conservative to avoid breaking content:
    - remove citation superscripts
    - remove navboxes
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        for sup in soup.select("sup.reference"):
            sup.decompose()
        for el in soup.select("table.navbox, div.navbox, div.navbox-styles"):
            el.decompose()
        return str(soup)
    except Exception:
        # Regex fallback (not perfect, but avoids extra deps).
        html = re.sub(r"<sup[^>]*class=\"reference\"[^>]*>.*?</sup>", "", html, flags=re.DOTALL)
        html = re.sub(r"<table[^>]*class=\"navbox\"[^>]*>.*?</table>", "", html, flags=re.DOTALL)
        return html


def _looks_like_disambiguation(html: str) -> bool:
    lowered = html.lower()
    return (
        "id=\"disambigbox\"" in lowered
        or "dmbox-disambig" in lowered
        or "mw-disambig" in lowered
    )


def _extract_disambiguation_links(html: str, *, max_links: int = 25) -> list[tuple[str, str]]:
    """
    Best-effort extraction of options from a disambiguation page.
    Returns list of (text, href).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[str, str]] = []
        for a in soup.select(".mw-parser-output a[href^='/wiki/']"):
            href = a.get("href")
            text = a.get_text(" ", strip=True)
            if not href or not text:
                continue
            if href.startswith("/wiki/Help:") or href.startswith("/wiki/Special:"):
                continue
            out.append((text, href))
            if len(out) >= max_links:
                break
        return out
    except Exception:
        return []


def render_wikipedia_markdown(
    *,
    title: str,
    canonical_url: str,
    host: str,
    body_markdown: str,
    truncated: bool,
) -> str:
    lines: list[str] = []
    lines.append("# Wikipedia Article")
    lines.append(f"Title: {title} Link: {canonical_url} Source: {host}".strip())
    lines.append("")
    lines.append(body_markdown.strip())
    lines.append("")
    if truncated:
        lines.append(f"_Content truncated. View full article: {canonical_url}_")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


class WikipediaApiClient:
    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def fetch_parsed_html(self, target: WikipediaTarget) -> dict[str, Any]:
        params: dict[str, Any] = {
            "action": "parse",
            "page": target.title,
            "prop": "text",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "disableeditsection": "1",
            "disabletoc": "1",
            "disablelimitreport": "1",
            "maxlag": "5",
        }
        headers = {"User-Agent": _default_user_agent()}
        resp = await self._http.get(target.api_base_url, params=params, headers=headers)

        # Respect maxlag backoff if server returns 503 with Retry-After.
        if resp.status_code == 503:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after else 2
            except Exception:
                delay = 2
            await anyio.sleep(min(max(delay, 1), 30))
            resp = await self._http.get(target.api_base_url, params=params, headers=headers)

        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise WikipediaError("Wikipedia API response was not a JSON object.")
        if "error" in data and data["error"]:
            err = data["error"]
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("info") or err.get("code") or "")
            raise WikipediaError(msg or "Wikipedia API returned an error.")
        return data


async def fetch_wikipedia_article_markdown(
    url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_chars: int | None = None,
) -> str:
    target = parse_wikipedia_url(url)

    if max_chars is None:
        try:
            max_chars = int(os.environ.get("WIKIPEDIA_MAX_CHARS", "50000"))
        except Exception:
            max_chars = 50_000
    if max_chars <= 0:
        max_chars = 50_000

    async def _run(client: httpx.AsyncClient) -> str:
        api = WikipediaApiClient(http_client=client)
        data = await api.fetch_parsed_html(target)
        parse_obj = data.get("parse")
        if not isinstance(parse_obj, dict):
            raise WikipediaError("Missing parse payload.")

        title = str(parse_obj.get("title") or target.title)
        text_obj = parse_obj.get("text")
        html = ""
        if isinstance(text_obj, str):
            html = text_obj
        elif isinstance(text_obj, dict):
            # Some wikis may return {"*": "<html>"}; handle defensively.
            html = str(text_obj.get("*") or "")
        # Release the parsed JSON container early (can be large).
        parse_obj = {}
        text_obj = None
        if not html.strip():
            raise WikipediaError("Empty article body.")

        if _looks_like_disambiguation(html):
            options = _extract_disambiguation_links(html)
            lines: list[str] = []
            lines.append("# Wikipedia Disambiguation")
            lines.append(f"Title: {title} Link: {target.canonical_url} Source: {target.host}".strip())
            lines.append("")
            if options:
                lines.append("Possible meanings / pages:")
                for text, href in options:
                    lines.append(f"- {text}: https://{target.host}{href}")
                lines.append("")
            lines.append("_This appears to be a disambiguation page._")
            lines.append("")
            # Drop large buffers before returning.
            html = ""
            return "\n".join(lines).strip() + "\n"

        cleaned_html = _strip_wikipedia_html_noise(html)
        # Drop raw HTML as soon as we have a cleaned version.
        html = ""
        md = extract_content_as_markdown(cleaned_html)
        cleaned_html = ""
        md = sanitize_markdown(md)

        truncated = False
        if len(md) > max_chars:
            md = md[:max_chars].rstrip() + "\n\n…(truncated)\n"
            truncated = True

        return render_wikipedia_markdown(
            title=title,
            canonical_url=target.canonical_url,
            host=target.host,
            body_markdown=md,
            truncated=truncated,
        )

    if http_client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return await _run(client)

    return await _run(http_client)
