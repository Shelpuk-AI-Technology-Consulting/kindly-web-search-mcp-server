from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestGitHubIssues(unittest.TestCase):
    def test_parse_github_issue_url(self) -> None:
        from kindly_web_search_mcp_server.content.github_issues import (
            GitHubIssueTarget,
            parse_github_issue_url,
        )

        target = parse_github_issue_url(
            "https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/issues/26"
        )
        self.assertEqual(
            target, GitHubIssueTarget(owner="nextlevelbuilder", repo="ui-ux-pro-max-skill", number=26)
        )

        target2 = parse_github_issue_url(
            "https://github.com/owner/repo/issues/123?foo=bar#issuecomment-1"
        )
        self.assertEqual(target2.owner, "owner")
        self.assertEqual(target2.repo, "repo")
        self.assertEqual(target2.number, 123)

    def test_parse_rejects_non_issue_urls(self) -> None:
        from kindly_web_search_mcp_server.content.github_issues import (
            GitHubIssueError,
            parse_github_issue_url,
        )

        with self.assertRaises(GitHubIssueError):
            parse_github_issue_url("https://github.com/owner/repo/pull/1")

        with self.assertRaises(GitHubIssueError):
            parse_github_issue_url("https://example.com/owner/repo/issues/1")

    def test_render_markdown_structure(self) -> None:
        from kindly_web_search_mcp_server.content.github_issues import render_issue_thread_markdown

        issue = {
            "title": "Bug: Something breaks",
            "body": "Issue body",
            "state": "OPEN",
            "createdAt": "2026-01-02T00:00:00Z",
            "url": "https://github.com/o/r/issues/1",
            "author": {"login": "alice"},
            "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 2}}],
            "comments": {"totalCount": 1},
        }
        comments = [
            {
                "body": "Try this fix",
                "createdAt": "2026-01-02T01:00:00Z",
                "url": "https://github.com/o/r/issues/1#issuecomment-1",
                "author": {"login": "bob"},
                "reactionGroups": [{"content": "THUMBS_UP", "users": {"totalCount": 5}}],
            }
        ]

        md = render_issue_thread_markdown(issue=issue, comments=comments, total_comments=1, truncated=False)
        self.assertIn("# Question", md)
        self.assertIn("# Answers", md)
        self.assertIn("Bug: Something breaks", md)
        self.assertIn("Likes: 2", md)
        self.assertIn("## Answer 1", md)
        self.assertIn("Likes: 5", md)
        self.assertIn("Permalink:", md)


if __name__ == "__main__":
    unittest.main()
