from __future__ import annotations

import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestContentResolverUniversalFallback(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_falls_back_to_universal_loader(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.load_url_as_markdown",
            new_callable=AsyncMock,
        ) as mock_universal:
            mock_universal.return_value = "# Page\n\nHello"
            out = await resolve_page_content_markdown("https://example.com")

        self.assertEqual(out, "# Page\n\nHello")

    async def test_resolver_uses_stackexchange_when_applicable(self) -> None:
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            return_value=("stackoverflow", 123),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_stackexchange_thread_markdown",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = "# Question\n..."
            out = await resolve_page_content_markdown("https://stackoverflow.com/questions/123/x")

        self.assertEqual(out, "# Question\n...")

    async def test_resolver_uses_github_issue_when_applicable(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_issue_url",
            return_value=("owner", "repo", 1),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_github_issue_thread_markdown",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = "# Question\n...\n# Answers\n"
            out = await resolve_page_content_markdown("https://github.com/owner/repo/issues/1")

        self.assertEqual(out, "# Question\n...\n# Answers\n")

    async def test_resolver_falls_back_when_github_fetch_fails(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_issue_url",
            return_value=("owner", "repo", 1),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_github_issue_thread_markdown",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.load_url_as_markdown",
            new_callable=AsyncMock,
        ) as mock_universal:
            mock_universal.return_value = "# Page\n\nFallback"
            out = await resolve_page_content_markdown("https://github.com/owner/repo/issues/1")

        self.assertEqual(out, "# Page\n\nFallback")

    async def test_resolver_uses_github_discussion_when_applicable(self) -> None:
        from kindly_web_search_mcp_server.content.github_issues import GitHubIssueError
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_issue_url",
            side_effect=GitHubIssueError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_discussion_url",
            return_value=("owner", "repo", 1),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_github_discussion_thread_markdown",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = "# Discussion\n..."
            out = await resolve_page_content_markdown("https://github.com/owner/repo/discussions/1")

        self.assertEqual(out, "# Discussion\n...")

    async def test_resolver_falls_back_when_github_discussion_fetch_fails(self) -> None:
        from kindly_web_search_mcp_server.content.github_issues import GitHubIssueError
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_issue_url",
            side_effect=GitHubIssueError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_discussion_url",
            return_value=("owner", "repo", 1),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_github_discussion_thread_markdown",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.load_url_as_markdown",
            new_callable=AsyncMock,
        ) as mock_universal:
            mock_universal.return_value = "# Page\n\nFallback"
            out = await resolve_page_content_markdown("https://github.com/owner/repo/discussions/1")

        self.assertEqual(out, "# Page\n\nFallback")

    async def test_resolver_uses_wikipedia_when_applicable(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import StackExchangeError
        from kindly_web_search_mcp_server.content.github_issues import GitHubIssueError
        from kindly_web_search_mcp_server.content.github_discussions import GitHubDiscussionError
        from kindly_web_search_mcp_server.content.resolver import resolve_page_content_markdown

        with patch(
            "kindly_web_search_mcp_server.content.resolver.parse_stackexchange_url",
            side_effect=StackExchangeError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_issue_url",
            side_effect=GitHubIssueError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_github_discussion_url",
            side_effect=GitHubDiscussionError("nope"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.parse_wikipedia_url",
            return_value=("api", "canon", "host", "title"),
        ), patch(
            "kindly_web_search_mcp_server.content.resolver.fetch_wikipedia_article_markdown",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = "# Wikipedia Article\n..."
            out = await resolve_page_content_markdown("https://en.wikipedia.org/wiki/Pet_door")

        self.assertEqual(out, "# Wikipedia Article\n...")


if __name__ == "__main__":
    unittest.main()
