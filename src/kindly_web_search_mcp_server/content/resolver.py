from __future__ import annotations

from .stackexchange import (
    StackExchangeError,
    fetch_stackexchange_thread_markdown,
    parse_stackexchange_url,
)
from .github_issues import (
    GitHubIssueError,
    fetch_github_issue_thread_markdown,
    parse_github_issue_url,
)
from .github_discussions import (
    GitHubDiscussionError,
    fetch_github_discussion_thread_markdown,
    parse_github_discussion_url,
)
from .wikipedia import (
    WikipediaError,
    fetch_wikipedia_article_markdown,
    parse_wikipedia_url,
)
from .arxiv import (
    ArxivError,
    fetch_arxiv_paper_markdown,
    parse_arxiv_url,
)
from ..scrape.universal_html import load_url_as_markdown
from ..utils.diagnostics import Diagnostics


async def resolve_page_content_markdown(
    url: str,
    *,
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """Resolve a URL to LLM-ready Markdown if supported.

    Stage 1: StackExchange API (StackOverflow + stackexchange network).
    Stage 2: GitHub Issue API (GitHub GraphQL).
    Stage 3: Wikipedia API (MediaWiki Action API).
    Stage 4: arXiv (Atom API + PDF → Markdown).
    Stage 5: Universal HTML loader fallback (headless Nodriver).
    """
    if diagnostics:
        diagnostics.emit("resolver.start", "Resolving URL", {"url": url})

    try:
        # Validate we can parse as StackExchange first.
        parse_stackexchange_url(url)
    except StackExchangeError:
        pass
    else:
        if diagnostics:
            diagnostics.emit("resolver.route", "Matched StackExchange URL", {"handler": "stackexchange"})
        try:
            return await fetch_stackexchange_thread_markdown(url)
        except Exception as e:
            if diagnostics:
                diagnostics.emit(
                    "resolver.error",
                    "StackExchange handler failed",
                    {"handler": "stackexchange", "error": type(e).__name__},
                )
            # Best-effort: return a short Markdown error note (no secrets).
            return f"_Failed to retrieve StackExchange content: {type(e).__name__}_\n\nSource: {url}\n"

    try:
        parse_github_issue_url(url)
    except GitHubIssueError:
        pass
    else:
        if diagnostics:
            diagnostics.emit("resolver.route", "Matched GitHub Issue URL", {"handler": "github_issue"})
        try:
            return await fetch_github_issue_thread_markdown(url)
        except Exception:
            if diagnostics:
                diagnostics.emit(
                    "resolver.fallback",
                    "GitHub Issue handler failed; falling back to HTML",
                    {"handler": "github_issue"},
                )
            # Prefer falling back to HTML loader for resilience (e.g., missing token, rate-limit).
            fallback = await load_url_as_markdown(url, diagnostics=diagnostics)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve GitHub Issue content._\n\nSource: {url}\n"

    try:
        parse_github_discussion_url(url)
    except GitHubDiscussionError:
        pass
    else:
        if diagnostics:
            diagnostics.emit("resolver.route", "Matched GitHub Discussion URL", {"handler": "github_discussion"})
        try:
            return await fetch_github_discussion_thread_markdown(url)
        except Exception:
            if diagnostics:
                diagnostics.emit(
                    "resolver.fallback",
                    "GitHub Discussion handler failed; falling back to HTML",
                    {"handler": "github_discussion"},
                )
            fallback = await load_url_as_markdown(url, diagnostics=diagnostics)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve GitHub Discussion content._\n\nSource: {url}\n"

    try:
        parse_wikipedia_url(url)
    except WikipediaError:
        pass
    else:
        if diagnostics:
            diagnostics.emit("resolver.route", "Matched Wikipedia URL", {"handler": "wikipedia"})
        try:
            return await fetch_wikipedia_article_markdown(url)
        except Exception:
            if diagnostics:
                diagnostics.emit(
                    "resolver.fallback",
                    "Wikipedia handler failed; falling back to HTML",
                    {"handler": "wikipedia"},
                )
            fallback = await load_url_as_markdown(url, diagnostics=diagnostics)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve Wikipedia content._\n\nSource: {url}\n"

    try:
        parse_arxiv_url(url)
    except ArxivError:
        pass
    else:
        if diagnostics:
            diagnostics.emit("resolver.route", "Matched arXiv URL", {"handler": "arxiv"})
        try:
            return await fetch_arxiv_paper_markdown(url)
        except Exception as e:
            if diagnostics:
                diagnostics.emit(
                    "resolver.error",
                    "arXiv handler failed",
                    {"handler": "arxiv", "error": type(e).__name__},
                )
            # arXiv is PDF-based and the universal HTML loader intentionally skips PDFs, so
            # we return a short Markdown error rather than falling back.
            return f"_Failed to retrieve arXiv content: {type(e).__name__}_\n\nSource: {url}\n"

    if diagnostics:
        diagnostics.emit("resolver.route", "Falling back to universal HTML", {"handler": "universal_html"})
    return await load_url_as_markdown(url, diagnostics=diagnostics)
