from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, TextIO

_TRUTHY = {"1", "true", "yes", "on"}
_MASK_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "BEARER")

# Credentials in a URL's userinfo component, e.g. `http://user:pass@proxy:8080`.
# The name-based hints above cannot catch these: proxy variables and CLI flags carry
# no secret marker in their names while their values routinely do.
#
# The character class admits `@` so the match runs to the *last* `@` before the path,
# which covers passwords containing an unescaped `@` (`http://user:pa@ss@host`). A
# stricter class stopping at the first `@` leaves the remainder of such a password
# exposed. The trade-off is that a pathological value may over-redact, which fails
# closed. `/` and whitespace still bound the match, so an `@` in a path is untouched.
_URL_USERINFO_RE = re.compile(r"://[^/\s]*@")
_REDACTED_USERINFO = "://***@"

MAX_SAMPLE_CHARS = 2000
MAX_STDERR_CHARS = 4000
MAX_LINE_CHARS = 8000


def diagnostics_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = (source.get("KINDLY_DIAGNOSTICS") or "").strip().lower()
    return raw in _TRUTHY


def new_request_id() -> str:
    return str(uuid.uuid4())


def redact_url_credentials(text: str) -> str:
    """Strip credentials from any URL userinfo component in ``text``

    Removes the ``user:pass@`` portion of a URL while leaving the scheme, host,
    and port readable, so diagnostics emitted to stderr stay useful for debugging
    proxy routing without disclosing secrets. Values containing no credentialed
    URL are returned unchanged.

    Args:
        text: Arbitrary text that may embed one or more URLs, such as an
            environment variable value or a command-line argument.

    Returns:
        The text with every URL userinfo component replaced by ``***``.
    """
    return _URL_USERINFO_RE.sub(_REDACTED_USERINFO, text)


def mask_env_values(env: Mapping[str, str]) -> dict[str, str]:
    """Mask secrets in an environment snapshot bound for diagnostics output

    Applies two complementary rules. Variables whose *name* signals a secret are
    replaced wholesale with their length, since no part of the value is safe to
    show. Every other value keeps its content but has any URL credentials removed,
    because variables such as ``HTTP_PROXY`` carry no secret marker in their name
    yet routinely embed ``user:pass@`` in their value.

    Args:
        env: The environment snapshot to mask. ``None`` values are treated as
            empty strings.

    Returns:
        A new mapping with the same keys and masked values.
    """
    masked: dict[str, str] = {}
    for key, value in env.items():
        raw = "" if value is None else str(value)
        if any(hint in key.upper() for hint in _MASK_HINTS):
            masked[key] = f"*** ({len(raw)})"
        else:
            # Redact only the userinfo rather than the whole value: these snapshots
            # exist to debug proxy routing, so the host and port must stay readable.
            masked[key] = redact_url_credentials(raw)
    return masked


def truncate_text(text: str | None, limit: int) -> tuple[str, bool, int]:
    if text is None:
        return "", False, 0
    raw = str(text)
    if len(raw) <= limit:
        return raw, False, len(raw)
    return raw[:limit] + "...(truncated)", True, len(raw)


def sample_data(text: str | None, limit: int) -> dict[str, Any]:
    sample, truncated, length = truncate_text(text, limit)
    return {
        "sample": sample,
        "sample_len": length,
        "sample_truncated": truncated,
    }


def _apply_line_limit(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return {
            "request_id": entry.get("request_id"),
            "stage": entry.get("stage"),
            "msg": entry.get("msg"),
            "elapsed_ms": entry.get("elapsed_ms"),
            "line_truncated": True,
            "data": {"note": "diagnostic payload contained non-serializable data"},
        }
    if len(payload) <= MAX_LINE_CHARS:
        return entry
    return {
        "request_id": entry.get("request_id"),
        "stage": entry.get("stage"),
        "msg": entry.get("msg"),
        "elapsed_ms": entry.get("elapsed_ms"),
        "line_truncated": True,
        "data": {
            "note": "diagnostic payload truncated",
            "original_len": len(payload),
        },
    }


def emit_diagnostic(entry: dict[str, Any], *, stream: TextIO | None = None) -> None:
    try:
        target = stream or sys.stderr
        payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
        target.write(f"KINDLY_DIAG {payload}\n")
        target.flush()
    except Exception:
        return


@dataclass
class Diagnostics:
    request_id: str
    enabled: bool
    stream: TextIO | None = None
    context: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, stage: str, msg: str, data: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        elapsed_ms = int((time.monotonic() - self.started) * 1000)
        merged: dict[str, Any] = dict(self.context)
        if data:
            merged.update(data)
        entry = {
            "request_id": self.request_id,
            "stage": stage,
            "msg": msg,
            "elapsed_ms": elapsed_ms,
            "data": merged,
        }
        entry = _apply_line_limit(entry)
        self.entries.append(entry)
        emit_diagnostic(entry, stream=self.stream)
