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


async def resolve_page_content_markdown(url: str) -> str | None:
    """Resolve a URL to LLM-ready Markdown if supported.

    Stage 1: StackExchange API (StackOverflow + stackexchange network).
    Stage 2: GitHub Issue API (GitHub GraphQL).
    Stage 3: Wikipedia API (MediaWiki Action API).
    Stage 4: arXiv (Atom API + PDF → Markdown).
    Stage 5: Universal HTML loader fallback (headless Nodriver).
    """
    try:
        # Validate we can parse as StackExchange first.
        parse_stackexchange_url(url)
    except StackExchangeError:
        pass
    else:
        try:
            return await fetch_stackexchange_thread_markdown(url)
        except Exception as e:
            # Best-effort: return a short Markdown error note (no secrets).
            return f"_Failed to retrieve StackExchange content: {type(e).__name__}_\n\nSource: {url}\n"

    try:
        parse_github_issue_url(url)
    except GitHubIssueError:
        pass
    else:
        try:
            return await fetch_github_issue_thread_markdown(url)
        except Exception:
            # Prefer falling back to HTML loader for resilience (e.g., missing token, rate-limit).
            fallback = await load_url_as_markdown(url)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve GitHub Issue content._\n\nSource: {url}\n"

    try:
        parse_github_discussion_url(url)
    except GitHubDiscussionError:
        pass
    else:
        try:
            return await fetch_github_discussion_thread_markdown(url)
        except Exception:
            fallback = await load_url_as_markdown(url)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve GitHub Discussion content._\n\nSource: {url}\n"

    try:
        parse_wikipedia_url(url)
    except WikipediaError:
        pass
    else:
        try:
            return await fetch_wikipedia_article_markdown(url)
        except Exception:
            fallback = await load_url_as_markdown(url)
            if fallback is not None:
                return fallback
            return f"_Failed to retrieve Wikipedia content._\n\nSource: {url}\n"

    try:
        parse_arxiv_url(url)
    except ArxivError:
        pass
    else:
        try:
            return await fetch_arxiv_paper_markdown(url)
        except Exception as e:
            # arXiv is PDF-based and the universal HTML loader intentionally skips PDFs, so
            # we return a short Markdown error rather than falling back.
            return f"_Failed to retrieve arXiv content: {type(e).__name__}_\n\nSource: {url}\n"

    return await load_url_as_markdown(url)
