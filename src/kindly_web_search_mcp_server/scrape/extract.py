from __future__ import annotations

import html as _html
import re
from typing import Callable

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

try:
    from markdownify import markdownify as md  # type: ignore
except Exception:  # pragma: no cover
    md = None  # type: ignore


def _strip_tags_keep_text(raw_html: str) -> str:
    # Remove script/style blocks first to avoid leaking JS/CSS into output.
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html or "")
    # Replace common block-level tags with newlines.
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n\n", cleaned)
    cleaned = re.sub(r"(?i)</div\s*>", "\n\n", cleaned)
    cleaned = re.sub(r"(?i)</li\s*>", "\n", cleaned)
    # Drop remaining tags.
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = _html.unescape(cleaned)
    # Normalize whitespace.
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _simple_html_to_markdown(raw_html: str) -> str:
    """
    Very small HTML→Markdown fallback.

    This is used only when optional dependencies (BeautifulSoup/markdownify) are
    not available. The output is "good enough" Markdown (plain text + headings).
    """
    h = raw_html or ""
    # Convert headings into ATX markdown.
    for level in range(1, 7):
        pattern = rf"(?is)<h{level}[^>]*>(.*?)</h{level}>"
        repl = lambda m, lvl=level: ("\n" + ("#" * lvl) + " " + _strip_tags_keep_text(m.group(1)) + "\n\n")
        h = re.sub(pattern, repl, h)
    # Convert list items.
    h = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: f"- {_strip_tags_keep_text(m.group(1))}\n", h)
    # Convert paragraphs.
    h = re.sub(r"(?is)<p[^>]*>(.*?)</p>", lambda m: f"{_strip_tags_keep_text(m.group(1))}\n\n", h)
    # Final cleanup.
    out = _strip_tags_keep_text(h)
    return out

def extract_content_as_markdown(html: str) -> str:
    """
    Extracts the main content from HTML, cleans it, and converts it to Markdown.
    """
    # Preferred path: BeautifulSoup + markdownify for higher-fidelity conversion.
    if BeautifulSoup is not None and md is not None:
        soup = BeautifulSoup(html, "html.parser")

        # Remove script, style, header, footer, and nav elements
        for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
            element.decompose()

        # Try to find the main content, falling back to the body
        main_content = soup.find("main") or soup.find("article") or soup.find("body")

        if main_content:
            # Convert the cleaned HTML to Markdown
            # Strip out unwanted tags and configure markdownify
            markdown_text = md(str(main_content), heading_style="ATX", strip=["a", "img"])
            return markdown_text
        return "Could not extract main content."

    # Fallback path: best-effort extraction without optional deps.
    return _simple_html_to_markdown(html)
