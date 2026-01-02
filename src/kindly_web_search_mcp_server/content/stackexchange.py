from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx


STACKEXCHANGE_API_BASE_URL = "https://api.stackexchange.com/2.3"

# Default to the official named filter that includes `body` (HTML) for both questions and answers.
# If `body_markdown` is desired, provide a custom filter via `STACKEXCHANGE_FILTER`.
DEFAULT_STACKEXCHANGE_FILTER = "withbody"


@dataclass(frozen=True)
class StackExchangeTarget:
    site: str
    question_id: int | None
    answer_id: int | None


class StackExchangeError(RuntimeError):
    pass


def _derive_site_parameter(host: str) -> str | None:
    host = host.lower()

    # Meta exception: meta.stackexchange.com -> site=meta (per provided docs)
    if host == "meta.stackexchange.com":
        return "meta"

    # Meta communities: meta.<community>.com -> meta.<community>
    if host.startswith("meta.") and host.endswith(".com"):
        return host.removesuffix(".com")

    # stackexchange subdomains: <community>.stackexchange.com -> <community>
    if host.endswith(".stackexchange.com"):
        return host[: -len(".stackexchange.com")].split(".")[0]

    # Common communities: stackoverflow.com, superuser.com, serverfault.com, askubuntu.com, etc.
    if host.endswith(".com"):
        return host.removesuffix(".com")

    return None


_QUESTION_RE = re.compile(r"/(?:questions|q)/(\d+)(?:/|$)")
_ANSWER_RE = re.compile(r"/a/(\d+)(?:/|$)")


def parse_stackexchange_url(url: str) -> StackExchangeTarget:
    """Parse a StackExchange network URL to determine site and question/answer id."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise StackExchangeError("URL has no hostname.")

    site = _derive_site_parameter(host)
    if not site:
        raise StackExchangeError(f"Unsupported StackExchange host: {host}")

    path = parsed.path or ""

    m_q = _QUESTION_RE.search(path)
    if m_q:
        return StackExchangeTarget(site=site, question_id=int(m_q.group(1)), answer_id=None)

    m_a = _ANSWER_RE.search(path)
    if m_a:
        return StackExchangeTarget(site=site, question_id=None, answer_id=int(m_a.group(1)))

    raise StackExchangeError("URL is not a recognized StackExchange question/answer URL.")


def _stackexchange_params(site: str, *, filter_id: str) -> dict[str, str]:
    params: dict[str, str] = {"site": site, "filter": filter_id}
    key = os.environ.get("STACKEXCHANGE_KEY", "").strip()
    if key:
        params["key"] = key
    return params


def _ensure_dict_json(resp: httpx.Response) -> dict[str, Any]:
    data = resp.json()
    if not isinstance(data, dict):
        raise StackExchangeError("StackExchange API response was not a JSON object.")
    return data


def _epoch_to_iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def render_thread_markdown(question: dict[str, Any], answers: list[dict[str, Any]]) -> str:
    """Render a StackExchange Q&A thread to deterministic Markdown."""
    def post_body_markdown(post: dict[str, Any]) -> str:
        # Prefer `body_markdown` (raw Markdown); fall back to `body` (HTML) and convert.
        body_md = post.get("body_markdown")
        if isinstance(body_md, str) and body_md.strip():
            return html.unescape(body_md)

        body_html = post.get("body")
        if not isinstance(body_html, str) or not body_html.strip():
            return ""

        raw_html = html.unescape(body_html)
        try:
            from markdownify import markdownify as _markdownify  # type: ignore

            return _markdownify(raw_html)
        except Exception:
            # As a last resort return HTML; better than empty content.
            return raw_html

    title = question.get("title") or ""
    link = question.get("link") or ""
    score = question.get("score")
    owner = question.get("owner") if isinstance(question.get("owner"), dict) else {}
    owner_link = owner.get("link") or ""
    created = _epoch_to_iso(question.get("creation_date"))
    body_md = post_body_markdown(question)

    lines: list[str] = []
    lines.append("# Question")
    lines.append(f"Question: {title}".strip())
    lines.append(
        f"Link: {link} Author: ({owner_link}) Date: {created} Score: {score}".strip()
    )
    lines.append("")
    lines.append(body_md)
    lines.append("")
    lines.append("# Answers")

    def sort_key(a: dict[str, Any]) -> tuple[int, int]:
        accepted = bool(a.get("is_accepted"))
        score_val = a.get("score")
        try:
            score_int = int(score_val)
        except Exception:
            score_int = 0
        # accepted first, then higher scores first
        return (0 if accepted else 1, -score_int)

    sorted_answers = sorted(answers, key=sort_key)
    for idx, ans in enumerate(sorted_answers, start=1):
        accepted = bool(ans.get("is_accepted"))
        ans_owner = ans.get("owner") if isinstance(ans.get("owner"), dict) else {}
        ans_author = ans_owner.get("display_name") or ""
        ans_created = _epoch_to_iso(ans.get("creation_date"))
        ans_score = ans.get("score")
        ans_body = post_body_markdown(ans)

        header = f"## Answer {idx}"
        if accepted:
            header += " (Accepted Solution)"
        lines.append(header)
        meta = f"Score: {ans_score}"
        if accepted:
            meta += " | ✅ Marked as Correct"
        meta += f" Author: {ans_author} Date: {ans_created}"
        lines.append(meta.strip())
        lines.append("")
        lines.append(ans_body)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


class StackExchangeApiClient:
    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._filter = (
            os.environ.get("STACKEXCHANGE_FILTER", DEFAULT_STACKEXCHANGE_FILTER).strip()
            or DEFAULT_STACKEXCHANGE_FILTER
        )

    async def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{STACKEXCHANGE_API_BASE_URL}{path}"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        data = _ensure_dict_json(resp)
        backoff = data.get("backoff")
        if isinstance(backoff, int) and backoff > 0:
            await anyio.sleep(backoff)
        return data

    async def resolve_question_id_from_answer(self, target: StackExchangeTarget) -> int:
        if target.answer_id is None:
            raise StackExchangeError("No answer_id to resolve.")
        params = _stackexchange_params(target.site, filter_id=self._filter)
        data = await self._get(f"/answers/{target.answer_id}/questions", params=params)
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            raise StackExchangeError("No parent question found for answer.")
        q = items[0]
        qid = q.get("question_id")
        if not isinstance(qid, int):
            raise StackExchangeError("Invalid parent question_id in response.")
        return qid

    async def fetch_question(self, target: StackExchangeTarget) -> dict[str, Any]:
        qid = target.question_id
        if qid is None and target.answer_id is not None:
            qid = await self.resolve_question_id_from_answer(target)
        if qid is None:
            raise StackExchangeError("Missing question id.")
        params = _stackexchange_params(target.site, filter_id=self._filter)
        data = await self._get(f"/questions/{qid}", params=params)
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            raise StackExchangeError("Question not found.")
        q = items[0]
        if not isinstance(q, dict):
            raise StackExchangeError("Invalid question payload.")
        return q

    async def fetch_all_answers(self, target: StackExchangeTarget) -> list[dict[str, Any]]:
        qid = target.question_id
        if qid is None and target.answer_id is not None:
            qid = await self.resolve_question_id_from_answer(target)
        if qid is None:
            raise StackExchangeError("Missing question id.")

        page = 1
        answers: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = _stackexchange_params(target.site, filter_id=self._filter)
            params.update(
                {
                    "page": page,
                    "pagesize": 100,
                    "order": "desc",
                    "sort": "votes",
                }
            )
            data = await self._get(f"/questions/{qid}/answers", params=params)
            items = data.get("items", [])
            if isinstance(items, list):
                answers.extend([a for a in items if isinstance(a, dict)])

            has_more = data.get("has_more")
            if has_more is True:
                page += 1
                continue
            break

        return answers


async def fetch_stackexchange_thread_markdown(
    url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    max_chars: int | None = None,
) -> str:
    """Fetch a StackExchange thread as Markdown (question + all answers)."""
    target = parse_stackexchange_url(url)

    if max_chars is None:
        try:
            max_chars = int(os.environ.get("STACKEXCHANGE_MAX_CHARS", "20000"))
        except Exception:
            max_chars = 20_000
    if max_chars <= 0:
        max_chars = 20_000

    async def _run(client: httpx.AsyncClient) -> str:
        api = StackExchangeApiClient(http_client=client)
        question = await api.fetch_question(target)
        answers = await api.fetch_all_answers(target)
        md = render_thread_markdown(question=question, answers=answers)
        if len(md) > max_chars:
            md = md[:max_chars].rstrip() + "\n\n…(truncated)\n"
        return md

    if http_client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return await _run(client)

    return await _run(http_client)
