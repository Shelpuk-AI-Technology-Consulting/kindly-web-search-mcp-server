from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from ..scrape.sanitize import sanitize_markdown


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubIssueError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubIssueTarget:
    owner: str
    repo: str
    number: int


_ISSUE_RE = re.compile(r"^/([^/]+)/([^/]+)/issues/(\d+)(?:/|$)")


def parse_github_issue_url(url: str) -> GitHubIssueTarget:
    """
    Parse a GitHub issue URL: https://github.com/<owner>/<repo>/issues/<number>

    Ignores query params and fragments.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise GitHubIssueError(f"Unsupported GitHub host: {host or '(missing)'}")

    path = parsed.path or ""
    m = _ISSUE_RE.match(path)
    if not m:
        raise GitHubIssueError("URL is not a recognized GitHub Issue URL.")

    owner, repo, num = m.group(1), m.group(2), m.group(3)
    try:
        number = int(num)
    except Exception as exc:
        raise GitHubIssueError("Invalid issue number.") from exc

    return GitHubIssueTarget(owner=owner, repo=repo, number=number)


def _iso(dt: Any) -> str:
    if isinstance(dt, str) and dt.strip():
        return dt
    if isinstance(dt, datetime):
        return dt.isoformat()
    return ""


def _reaction_count(reaction_groups: Any, content: str) -> int:
    """
    Extract a reaction count from GitHub GraphQL `reactionGroups`.

    `reactionGroups` is typically an array of objects:
    - content: "THUMBS_UP", "HEART", ...
    - users.totalCount: int
    """
    if not isinstance(reaction_groups, list):
        return 0
    for g in reaction_groups:
        if not isinstance(g, dict):
            continue
        if g.get("content") != content:
            continue
        users = g.get("users")
        if isinstance(users, dict):
            try:
                return int(users.get("totalCount") or 0)
            except Exception:
                return 0
    return 0


def render_issue_thread_markdown(
    *,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    total_comments: int | None = None,
    truncated: bool = False,
) -> str:
    title = str(issue.get("title") or "").strip()
    url = str(issue.get("url") or "").strip()
    state = str(issue.get("state") or "").strip()
    created_at = _iso(issue.get("createdAt"))
    author_login = ""
    author = issue.get("author")
    if isinstance(author, dict):
        author_login = str(author.get("login") or "").strip()

    issue_likes = _reaction_count(issue.get("reactionGroups"), "THUMBS_UP")
    issue_body = str(issue.get("body") or "")
    issue_body = sanitize_markdown(issue_body)

    lines: list[str] = []
    lines.append("# Question")
    if title:
        lines.append(f"Question: {title}")
    meta_parts = []
    if url:
        meta_parts.append(f"Link: {url}")
    if author_login:
        meta_parts.append(f"Author: @{author_login}")
    if created_at:
        meta_parts.append(f"Date: {created_at}")
    if state:
        meta_parts.append(f"State: {state}")
    meta_parts.append(f"Likes: {issue_likes}")
    lines.append(" ".join(meta_parts).strip())
    lines.append("")
    lines.append(issue_body.strip())
    lines.append("")
    lines.append("# Answers")

    for idx, c in enumerate(comments, start=1):
        c_author_login = ""
        c_author = c.get("author")
        if isinstance(c_author, dict):
            c_author_login = str(c_author.get("login") or "").strip()
        c_created = _iso(c.get("createdAt"))
        c_url = str(c.get("url") or "").strip()
        c_likes = _reaction_count(c.get("reactionGroups"), "THUMBS_UP")
        c_body = sanitize_markdown(str(c.get("body") or ""))

        lines.append(f"## Answer {idx}")
        meta = []
        if c_author_login:
            meta.append(f"Author: @{c_author_login}")
        if c_created:
            meta.append(f"Date: {c_created}")
        meta.append(f"Likes: {c_likes}")
        if c_url:
            meta.append(f"Permalink: {c_url}")
        lines.append(" | ".join(meta).strip())
        lines.append("")
        lines.append(c_body.strip())
        lines.append("")

    if truncated:
        shown = len(comments)
        if total_comments is None:
            lines.append(f"_Thread truncated: showing {shown} comments._")
        else:
            lines.append(f"_Thread truncated: showing {shown} of {total_comments} comments._")
        if url:
            lines.append(f"_View full thread: {url}_")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


class GitHubGraphqlClient:
    def __init__(self, *, http_client: httpx.AsyncClient, token: str) -> None:
        self._http = http_client
        self._token = token

    async def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        resp = await self._http.post(
            GITHUB_GRAPHQL_URL, json={"query": query, "variables": variables}, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GitHubIssueError("GitHub GraphQL response was not a JSON object.")
        if "errors" in data and data["errors"]:
            # Avoid including the full error payload (can be verbose); include message only.
            err0 = data["errors"][0] if isinstance(data["errors"], list) else None
            msg = ""
            if isinstance(err0, dict):
                msg = str(err0.get("message") or "")
            raise GitHubIssueError(msg or "GitHub GraphQL returned errors.")
        return data

    async def fetch_issue_with_comments(
        self,
        target: GitHubIssueTarget,
        *,
        max_comments: int = 50,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        query = """
        query ($owner: String!, $name: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              title
              body
              state
              createdAt
              url
              author { login }
              reactionGroups {
                content
                users { totalCount }
              }
              comments(first: 100, after: $cursor) {
                totalCount
                pageInfo { hasNextPage endCursor }
                nodes {
                  body
                  createdAt
                  url
                  author { login }
                  reactionGroups {
                    content
                    users { totalCount }
                  }
                }
              }
            }
          }
        }
        """

        comments: list[dict[str, Any]] = []
        cursor: str | None = None
        total_count: int = 0

        while True:
            variables: dict[str, Any] = {
                "owner": target.owner,
                "name": target.repo,
                "number": target.number,
                "cursor": cursor,
            }
            raw = await self._post(query, variables)
            repo = raw.get("data", {}).get("repository")
            if not isinstance(repo, dict):
                raise GitHubIssueError("Repository not found or not accessible.")
            issue = repo.get("issue")
            if not isinstance(issue, dict):
                raise GitHubIssueError("Issue not found or not accessible.")

            comments_obj = issue.get("comments")
            if not isinstance(comments_obj, dict):
                return issue, [], 0

            if isinstance(comments_obj.get("totalCount"), int):
                total_count = int(comments_obj["totalCount"])

            nodes = comments_obj.get("nodes", [])
            if isinstance(nodes, list):
                for n in nodes:
                    if isinstance(n, dict):
                        comments.append(n)
                        if len(comments) >= max_comments:
                            return issue, comments[:max_comments], total_count

            page_info = comments_obj.get("pageInfo", {})
            if not isinstance(page_info, dict):
                break
            has_next = bool(page_info.get("hasNextPage"))
            cursor_val = page_info.get("endCursor")
            cursor = str(cursor_val) if cursor_val else None
            if not has_next or not cursor:
                break

        return issue, comments, total_count


async def fetch_github_issue_thread_markdown(
    url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_comments: int | None = None,
    max_chars: int | None = None,
) -> str:
    target = parse_github_issue_url(url)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubIssueError("GITHUB_TOKEN is required for GitHub Issue retrieval.")

    if max_comments is None:
        try:
            max_comments = int(os.environ.get("GITHUB_MAX_COMMENTS", "50"))
        except Exception:
            max_comments = 50
    if max_comments <= 0:
        max_comments = 50

    if max_chars is None:
        try:
            max_chars = int(os.environ.get("GITHUB_MAX_CHARS", "20000"))
        except Exception:
            max_chars = 20_000
    if max_chars <= 0:
        max_chars = 20_000

    async def _run(client: httpx.AsyncClient) -> str:
        api = GitHubGraphqlClient(http_client=client, token=token)
        issue, comments, total = await api.fetch_issue_with_comments(target, max_comments=max_comments)
        truncated = total > len(comments)
        md = render_issue_thread_markdown(
            issue=issue, comments=comments, total_comments=total, truncated=truncated
        )
        if len(md) > max_chars:
            md = md[:max_chars].rstrip() + "\n\n…(truncated)\n"
        return md

    if http_client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return await _run(client)

    return await _run(http_client)
