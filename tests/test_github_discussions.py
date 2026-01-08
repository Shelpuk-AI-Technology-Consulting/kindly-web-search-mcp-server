from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestGitHubDiscussions(unittest.TestCase):
    def test_parse_github_discussion_url(self) -> None:
        from kindly_web_search_mcp_server.content.github_discussions import (
            GitHubDiscussionTarget,
            parse_github_discussion_url,
        )

        target = parse_github_discussion_url(
            "https://github.com/ultrafunkamsterdam/undetected-chromedriver/discussions/1798"
        )
        self.assertEqual(
            target,
            GitHubDiscussionTarget(
                owner="ultrafunkamsterdam", repo="undetected-chromedriver", number=1798
            ),
        )

        target2 = parse_github_discussion_url(
            "https://github.com/owner/repo/discussions/123?foo=bar#discussioncomment-1"
        )
        self.assertEqual(target2.owner, "owner")
        self.assertEqual(target2.repo, "repo")
        self.assertEqual(target2.number, 123)

    def test_parse_rejects_non_discussion_urls(self) -> None:
        from kindly_web_search_mcp_server.content.github_discussions import (
            GitHubDiscussionError,
            parse_github_discussion_url,
        )

        with self.assertRaises(GitHubDiscussionError):
            parse_github_discussion_url("https://github.com/owner/repo/issues/1")

        with self.assertRaises(GitHubDiscussionError):
            parse_github_discussion_url("https://github.com/owner/repo/discussions/categories")

        with self.assertRaises(GitHubDiscussionError):
            parse_github_discussion_url("https://example.com/owner/repo/discussions/1")

    def test_render_markdown_structure(self) -> None:
        from kindly_web_search_mcp_server.content.github_discussions import (
            render_discussion_thread_markdown,
        )

        discussion = {
            "id": "D_1",
            "title": "How do I do X?",
            "body": "Top post body",
            "createdAt": "2026-01-02T00:00:00Z",
            "updatedAt": "2026-01-03T00:00:00Z",
            "url": "https://github.com/o/r/discussions/1",
            "author": {"login": "alice"},
            "category": {"name": "Q&A", "slug": "q-a"},
            "upvoteCount": 7,
            "isAnswered": True,
            "answer": {"id": "C_1", "url": "https://github.com/o/r/discussions/1#discussioncomment-1"},
            "activeLockReason": None,
        }
        comments = [
            {
                "id": "C_1",
                "body": "This is the answer",
                "createdAt": "2026-01-02T01:00:00Z",
                "updatedAt": "2026-01-02T01:00:00Z",
                "url": "https://github.com/o/r/discussions/1#discussioncomment-1",
                "author": {"login": "bob"},
                "upvoteCount": 3,
                "_replies": [
                    {
                        "id": "R_1",
                        "body": "Thanks!",
                        "createdAt": "2026-01-02T02:00:00Z",
                        "updatedAt": "2026-01-02T02:00:00Z",
                        "url": "https://github.com/o/r/discussions/1#discussioncomment-2",
                        "author": None,
                        "upvoteCount": 1,
                    }
                ],
                "_replies_total_count": 3,
                "_replies_truncated": True,
            }
        ]

        md = render_discussion_thread_markdown(
            discussion=discussion,
            comments=comments,
            total_top_level_comments=1,
            total_messages_shown=2,
            truncated=False,
        )

        self.assertIn("# Discussion", md)
        self.assertIn("## Post", md)
        self.assertIn("## Messages", md)
        self.assertIn("### Message 1", md)
        self.assertIn("#### Reply 1.1", md)
        self.assertIn("✅ Answer", md)
        self.assertIn("Author: (deleted)", md)
        self.assertIn("Replies truncated", md)


if __name__ == "__main__":
    unittest.main()
