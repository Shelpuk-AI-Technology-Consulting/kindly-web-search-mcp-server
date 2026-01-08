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


class GitHubDiscussionError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubDiscussionTarget:
    owner: str
    repo: str
    number: int


_DISCUSSION_RE = re.compile(r"^/([^/]+)/([^/]+)/discussions/(\d+)(?:/|$)")


def parse_github_discussion_url(url: str) -> GitHubDiscussionTarget:
    """
    Parse a GitHub discussion URL: https://github.com/<owner>/<repo>/discussions/<number>

    Ignores query params and fragments.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise GitHubDiscussionError(f"Unsupported GitHub host: {host or '(missing)'}")

    path = parsed.path or ""
    m = _DISCUSSION_RE.match(path)
    if not m:
        raise GitHubDiscussionError("URL is not a recognized GitHub Discussion URL.")

    owner, repo, num = m.group(1), m.group(2), m.group(3)
    try:
        number = int(num)
    except Exception as exc:
        raise GitHubDiscussionError("Invalid discussion number.") from exc

    return GitHubDiscussionTarget(owner=owner, repo=repo, number=number)


def _iso(dt: Any) -> str:
    if isinstance(dt, str) and dt.strip():
        return dt
    if isinstance(dt, datetime):
        return dt.isoformat()
    return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def render_discussion_thread_markdown(
    *,
    discussion: dict[str, Any],
    comments: list[dict[str, Any]],
    total_top_level_comments: int | None = None,
    total_messages_shown: int | None = None,
    truncated: bool = False,
) -> str:
    title = str(discussion.get("title") or "").strip()
    url = str(discussion.get("url") or "").strip()
    created_at = _iso(discussion.get("createdAt"))
    updated_at = _iso(discussion.get("updatedAt"))
    category_name = ""
    category = discussion.get("category")
    if isinstance(category, dict):
        category_name = str(category.get("name") or "").strip()

    author_login = "(deleted)"
    author = discussion.get("author")
    if isinstance(author, dict):
        author_login = str(author.get("login") or "").strip() or "(deleted)"

    upvotes = _safe_int(discussion.get("upvoteCount"))
    is_answered = bool(discussion.get("isAnswered"))
    lock_reason = str(discussion.get("activeLockReason") or "").strip()
    answer_chosen_at = _iso(discussion.get("answerChosenAt"))
    answer_chosen_by = ""
    answer_chosen_by_obj = discussion.get("answerChosenBy")
    if isinstance(answer_chosen_by_obj, dict):
        answer_chosen_by = str(answer_chosen_by_obj.get("login") or "").strip()
    answer_id = ""
    answer = discussion.get("answer")
    if isinstance(answer, dict):
        answer_id = str(answer.get("id") or "").strip()

    body = sanitize_markdown(str(discussion.get("body") or ""))

    lines: list[str] = []
    lines.append("# Discussion")
    if title:
        lines.append(f"Discussion: {title}")
    meta_parts: list[str] = []
    if url:
        meta_parts.append(f"Link: {url}")
    if category_name:
        meta_parts.append(f"Category: {category_name}")
    if author_login:
        meta_parts.append(f"Author: @{author_login}" if author_login != "(deleted)" else "Author: (deleted)")
    if created_at:
        meta_parts.append(f"Created: {created_at}")
    if updated_at and updated_at != created_at:
        meta_parts.append(f"Updated: {updated_at}")
    if is_answered:
        meta_parts.append("Answered: yes")
        if answer_chosen_at:
            meta_parts.append(f"Answer chosen: {answer_chosen_at}")
        if answer_chosen_by:
            meta_parts.append(f"Answer chosen by: @{answer_chosen_by}")
    if lock_reason:
        meta_parts.append(f"Locked: {lock_reason}")
    meta_parts.append(f"Upvotes: {upvotes}")
    lines.append(" | ".join(meta_parts).strip())
    lines.append("")
    lines.append("## Post")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    lines.append("## Messages")
    lines.append("")

    message_count = 0
    for idx, c in enumerate(comments, start=1):
        c_author_login = "(deleted)"
        c_author = c.get("author")
        if isinstance(c_author, dict):
            c_author_login = str(c_author.get("login") or "").strip() or "(deleted)"
        c_created = _iso(c.get("createdAt"))
        c_updated = _iso(c.get("updatedAt"))
        c_url = str(c.get("url") or "").strip()
        c_upvotes = _safe_int(c.get("upvoteCount"))
        c_id = str(c.get("id") or "").strip()
        c_body = sanitize_markdown(str(c.get("body") or ""))

        message_count += 1
        lines.append(f"### Message {idx}")
        meta: list[str] = []
        if c_author_login:
            meta.append(f"Author: @{c_author_login}" if c_author_login != "(deleted)" else "Author: (deleted)")
        if c_created:
            meta.append(f"Created: {c_created}")
        if c_updated and c_updated != c_created:
            meta.append(f"Updated: {c_updated}")
            meta.append("Edited: yes")
        meta.append(f"Upvotes: {c_upvotes}")
        if c_id and answer_id and c_id == answer_id:
            meta.append("✅ Answer")
        if c_url:
            meta.append(f"Permalink: {c_url}")
        lines.append(" | ".join(meta).strip())
        lines.append("")
        lines.append(c_body.strip())
        lines.append("")

        replies = c.get("_replies")
        if isinstance(replies, list):
            for ridx, r in enumerate(replies, start=1):
                r_author_login = "(deleted)"
                r_author = r.get("author")
                if isinstance(r_author, dict):
                    r_author_login = str(r_author.get("login") or "").strip() or "(deleted)"
                r_created = _iso(r.get("createdAt"))
                r_updated = _iso(r.get("updatedAt"))
                r_url = str(r.get("url") or "").strip()
                r_upvotes = _safe_int(r.get("upvoteCount"))
                r_body = sanitize_markdown(str(r.get("body") or ""))

                message_count += 1
                lines.append(f"#### Reply {idx}.{ridx}")
                rmeta: list[str] = []
                if r_author_login:
                    rmeta.append(
                        f"Author: @{r_author_login}"
                        if r_author_login != "(deleted)"
                        else "Author: (deleted)"
                    )
                if r_created:
                    rmeta.append(f"Created: {r_created}")
                if r_updated and r_updated != r_created:
                    rmeta.append(f"Updated: {r_updated}")
                    rmeta.append("Edited: yes")
                rmeta.append(f"Upvotes: {r_upvotes}")
                if r_url:
                    rmeta.append(f"Permalink: {r_url}")
                lines.append(" | ".join(rmeta).strip())
                lines.append("")
                lines.append(r_body.strip())
                lines.append("")

            replies_total = _safe_int(c.get("_replies_total_count"))
            replies_truncated = bool(c.get("_replies_truncated"))
            if replies_truncated and replies_total:
                lines.append(
                    f"_Replies truncated: showing {len(replies)} of {replies_total} replies._"
                )
                lines.append("")

    if truncated:
        shown = total_messages_shown if total_messages_shown is not None else message_count
        total = total_top_level_comments
        if total is None:
            lines.append(f"_Thread truncated: showing {shown} messages._")
        else:
            lines.append(f"_Thread truncated: showing {shown} messages (top-level total: {total})._")
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
            raise GitHubDiscussionError("GitHub GraphQL response was not a JSON object.")
        if "errors" in data and data["errors"]:
            err0 = data["errors"][0] if isinstance(data["errors"], list) else None
            msg = ""
            if isinstance(err0, dict):
                msg = str(err0.get("message") or "")
            raise GitHubDiscussionError(msg or "GitHub GraphQL returned errors.")
        return data

    async def fetch_discussion_with_comments(
        self,
        target: GitHubDiscussionTarget,
        *,
        max_messages: int = 50,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], int, bool, int]:
        query = """
        query (
          $owner: String!,
          $name: String!,
          $number: Int!,
          $cursor: String,
          $commentsFirst: Int!,
          $repliesFirst: Int!
        ) {
          repository(owner: $owner, name: $name) {
            discussion(number: $number) {
              id
              number
              title
              url
              createdAt
              updatedAt
              isAnswered
              answerChosenAt
              answerChosenBy { login }
              answer { id url }
              activeLockReason
              upvoteCount
              category { name slug }
              author { login }
              body
              comments(first: $commentsFirst, after: $cursor) {
                totalCount
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  body
                  createdAt
                  updatedAt
                  url
                  upvoteCount
                  author { login }
                  replies(first: $repliesFirst) {
                    totalCount
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      id
                      body
                      createdAt
                      updatedAt
                      url
                      upvoteCount
                      author { login }
                    }
                  }
                }
              }
            }
          }
        }
        """

        comments: list[dict[str, Any]] = []
        cursor: str | None = None
        total_top_level_comments: int = 0
        discussion: dict[str, Any] | None = None
        messages_used = 0
        truncated = False
        has_next_page_seen = False
        replies_truncated_seen = False

        while True:
            remaining = max_messages - messages_used
            if remaining <= 0:
                break

            # Fetch fewer top-level comments when near the cap to reduce overfetching.
            comments_first = max(1, min(100, remaining))
            # Replies are best-effort: only fetch the first page per comment.
            replies_first = min(50, max_messages)

            variables: dict[str, Any] = {
                "owner": target.owner,
                "name": target.repo,
                "number": target.number,
                "cursor": cursor,
                "commentsFirst": comments_first,
                "repliesFirst": replies_first,
            }
            raw = await self._post(query, variables)
            repo = raw.get("data", {}).get("repository")
            if not isinstance(repo, dict):
                raise GitHubDiscussionError("Repository not found or not accessible.")
            d = repo.get("discussion")
            if not isinstance(d, dict):
                raise GitHubDiscussionError("Discussion not found or not accessible.")
            discussion = d

            comments_obj = d.get("comments")
            if not isinstance(comments_obj, dict):
                return d, [], 0, False, 0

            if isinstance(comments_obj.get("totalCount"), int):
                total_top_level_comments = int(comments_obj["totalCount"])

            nodes = comments_obj.get("nodes", [])
            if isinstance(nodes, list):
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    if messages_used >= max_messages:
                        break

                    # Count the top-level comment itself as one message.
                    messages_used += 1
                    reply_budget = max_messages - messages_used

                    replies_obj = node.get("replies")
                    replies_nodes: list[dict[str, Any]] = []
                    replies_total_count = 0
                    replies_truncated = False
                    if isinstance(replies_obj, dict):
                        replies_total_count = _safe_int(replies_obj.get("totalCount"))
                        raw_replies = replies_obj.get("nodes", [])
                        if isinstance(raw_replies, list):
                            for r in raw_replies:
                                if not isinstance(r, dict):
                                    continue
                                if reply_budget <= 0:
                                    break
                                replies_nodes.append(r)
                                messages_used += 1
                                reply_budget -= 1
                        if replies_total_count and replies_total_count > len(replies_nodes):
                            replies_truncated = True
                            replies_truncated_seen = True

                    node_with_replies = dict(node)
                    node_with_replies["_replies"] = replies_nodes
                    node_with_replies["_replies_total_count"] = replies_total_count
                    node_with_replies["_replies_truncated"] = replies_truncated
                    comments.append(node_with_replies)

            page_info = comments_obj.get("pageInfo", {})
            if not isinstance(page_info, dict):
                break
            has_next = bool(page_info.get("hasNextPage"))
            if has_next:
                has_next_page_seen = True
            cursor_val = page_info.get("endCursor")
            cursor = str(cursor_val) if cursor_val else None
            if not has_next or not cursor:
                break

        if discussion is None:
            raise GitHubDiscussionError("Discussion not found or not accessible.")
        if messages_used >= max_messages and (has_next_page_seen or replies_truncated_seen):
            truncated = True
        if total_top_level_comments and total_top_level_comments > len(comments):
            truncated = True
        if replies_truncated_seen:
            truncated = True
        return discussion, comments, total_top_level_comments, truncated, messages_used


async def fetch_github_discussion_thread_markdown(
    url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_comments: int | None = None,
    max_chars: int | None = None,
) -> str:
    target = parse_github_discussion_url(url)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubDiscussionError("GITHUB_TOKEN is required for GitHub Discussion retrieval.")

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
        discussion, comments, total, truncated, messages_used = await api.fetch_discussion_with_comments(
            target, max_messages=max_comments
        )

        md = render_discussion_thread_markdown(
            discussion=discussion,
            comments=comments,
            total_top_level_comments=total,
            total_messages_shown=messages_used,
            truncated=truncated,
        )
        if len(md) > max_chars:
            md = md[:max_chars].rstrip() + "\n\n…(truncated)\n"
        return md

    if http_client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return await _run(client)

    return await _run(http_client)
