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

#: Marker that distinguishes a deliberate diagnostics frame from whatever else a
#: child writes to standard error. **Defined here and nowhere else**: the worker
#: encodes with it and the parent's stream reader decodes with it, and before
#: E6-2 each of them spelled it out separately.
#:
#: The trailing space is part of the marker, not formatting. ``KINDLY_DIAGNOSTICS``
#: — the environment variable that turns diagnostics on — shares its stem, so a
#: sweep for the prefix without the space also matches that variable, in files the
#: allow-list has no reason to name. The count of those files is deliberately not
#: given: it moves whenever anything new reads the variable, and a stale figure here
#: is worse than none. ``tests/test_worker_frame_contract.py`` holds this value and
#: asserts the two identifiers cannot be confused.
FRAME_PREFIX = "KINDLY_DIAG "

#: Ceiling on one *unterminated* line the parent's stderr reader will buffer.
#:
#: Two constituencies, and a reader who knows only the first will lower it. The
#: first is the frame relation: it must exceed ``len(FRAME_PREFIX) +
#: MAX_LINE_CHARS`` (8012), or the parent would cut up a frame the worker
#: considers legal and file it as a parse error. The second, and the reason the
#: constant exists at all, is memory: a child that writes a long line and never
#: terminates it grew the reader's buffer without bound, measured at 640 KB.
#:
#: Twice the frame ceiling, which satisfies the first comfortably. The *tail* is
#: capped far lower, at :data:`MAX_STDERR_CHARS`, so a larger value here buys no
#: extra output — only headroom against the frame relation.
MAX_STDERR_LINE_CHARS = 16000

#: How much of a copied field the oversized-record fallback keeps. Small on
#: purpose: the fallback exists to say "a record was too big", and reproducing
#: the field that made it too big would defeat it. See :func:`apply_line_limit`.
_FALLBACK_FIELD_CHARS = 200


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


def _fallback_entry(entry: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Build the replacement record for one that cannot be emitted as it stands.

    The three copied fields are **truncated**, not copied verbatim. That is the
    whole point of this helper existing separately: the fallback used to copy
    ``stage`` and ``msg`` whole, so a record with a 50 000-character ``msg``
    produced a 50 156-character line while reporting ``line_truncated: True``.
    :data:`MAX_LINE_CHARS` then bounded only what *triggered* the fallback, never
    what the fallback wrote — which made the reader's own cap unsound, because a
    frame the emitter considered legal could still exceed it.

    The three text fields are bounded by :func:`truncate_text`, which coerces
    with ``str`` and so always yields a string. ``elapsed_ms`` is the one field
    copied without conversion, and it is the one that can still defeat the
    result: a sufficiently large integer raises ``ValueError`` inside
    ``json.dumps`` on the *fallback*, after the original was already rejected for
    the same reason. Unreachable from either emitter — both derive it from a
    monotonic clock — and guarded anyway, because this function's whole promise
    is that what it returns can be written, and a promise with one uninspected
    field is not one. So the result is verified rather than assumed, and degrades
    to a record that copies nothing if it still will not serialize.

    Args:
        entry: The record being replaced, read for its identifying fields.
        data: The ``data`` payload explaining why it was replaced.

    Returns:
        A record that is serializable and within :data:`MAX_LINE_CHARS`.
    """
    candidate = {
        "request_id": truncate_text(entry.get("request_id"), _FALLBACK_FIELD_CHARS)[0],
        "stage": truncate_text(entry.get("stage"), _FALLBACK_FIELD_CHARS)[0],
        "msg": truncate_text(entry.get("msg"), _FALLBACK_FIELD_CHARS)[0],
        "elapsed_ms": entry.get("elapsed_ms"),
        "line_truncated": True,
        "data": data,
    }
    # Verified, not assumed. `data` is this module's own literal and the three
    # text fields are bounded above, so only a copied `elapsed_ms` can reach
    # either branch.
    try:
        rendered = json.dumps(candidate, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = None
    if rendered is not None and len(rendered) <= MAX_LINE_CHARS:
        return candidate
    return {
        "request_id": None,
        "stage": None,
        "msg": None,
        "elapsed_ms": None,
        "line_truncated": True,
        "data": data,
    }


def apply_line_limit(entry: dict[str, Any]) -> dict[str, Any]:
    """Bound one diagnostics record to the frame ceiling.

    Public because both sides need it: the parent's :class:`Diagnostics` applies
    it before storing and writing, and the worker's ``_emit_diag`` applies it
    before encoding. The worker used to carry its own copy against a private
    ``_DIAG_LINE_LIMIT`` constant, kept in step by a comment — and the two had
    already diverged, because only this one handles a record that will not
    serialize at all.

    Args:
        entry: The record to bound.

    Returns:
        ``entry`` unchanged when it already fits, and otherwise a bounded
        replacement carrying ``line_truncated``.
    """
    # A record that will not serialize has no length to compare, so it is
    # replaced rather than measured. The worker's own copy raised here and
    # emitted nothing at all, which is indistinguishable from the stage never
    # having been reached.
    try:
        payload = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _fallback_entry(
            entry, {"note": "diagnostic payload contained non-serializable data"}
        )
    if len(payload) <= MAX_LINE_CHARS:
        return entry
    return _fallback_entry(
        entry,
        {"note": "diagnostic payload truncated", "original_len": len(payload)},
    )


def encode_frame(entry: dict[str, Any]) -> str:
    """Render one diagnostics record as a wire frame, without its terminator.

    The single encoder for the parent ⇄ worker protocol described in §4.3 of
    ``.system_design/TEST_SUITE.md``. Both writers call it: this module's
    :func:`emit_diagnostic` and the worker's ``_emit_diag``.

    No terminator is appended, because the two callers write through different
    primitives and each supplies its own — the worker's ``_safe_write_text``
    adds one unconditionally, and appending a second here would give it a blank
    line to strip.

    ``ensure_ascii`` keeps every frame free of multi-byte sequences, which is
    what makes a *frame* immune to being torn at a chunk boundary. The compact
    separators are not cosmetic either: they are what keeps a frame inside
    :data:`MAX_LINE_CHARS`.

    **This is a serializer and deliberately not the redaction point.** §7.1
    requires sanitization at the top of :meth:`Diagnostics.emit`, before the
    record reaches ``entries``, because ``entries`` is returned to the MCP caller
    as well as written to stderr. Redacting here would clean the stderr copy and
    leave the raw value in the response — the worse of the two paths, and the
    exact inversion §7.1 names. This function is the tempting place for it
    precisely because it is now the one both writers share.

    Args:
        entry: The record to encode. Bound it with :func:`apply_line_limit`
            first; this function does not, so a caller that must also *store*
            the bounded record is not made to bound it twice.

    Returns:
        The complete line, marker included, with no trailing newline.
    """
    return FRAME_PREFIX + json.dumps(entry, ensure_ascii=True, separators=(",", ":"))


def frame_payload(line: str) -> str | None:
    """Extract the payload of a frame line, or report that it is not one.

    The first half of the decoder. Three outcomes matter to the parent's line
    router and two functions express them without a result type: this one
    separates a frame from ordinary child output, and
    :func:`decode_frame_payload` separates a good frame from a malformed one.

    ``None`` rather than ``""`` for a non-frame line, and the difference is
    load-bearing: a line that is *only* the marker is a frame whose payload is
    empty, which is malformed and must be **sampled**, while a line that is not a
    frame is ordinary output and joins the tail. Both are falsy, so one falsy
    return value cannot carry both — ``""`` means "a frame, with nothing in it"
    and ``None`` means "not a frame".

    A genuinely blank line reaches neither: the router discards it before asking
    this function anything. An earlier draft of this paragraph claimed a blank
    line "must reach the stderr tail", which is not what the router does and was
    never what this distinction was for.

    The ``strip`` is load-bearing rather than tidy: it is what lets a
    ``\\r\\n``-terminated frame decode even though the carriage return is removed
    elsewhere. Ordinary output has no such second defence.

    Args:
        line: One complete line, already stripped of its terminator.

    Returns:
        The payload text, or ``None`` when the line is not a frame.
    """
    if not line.startswith(FRAME_PREFIX):
        return None
    return line[len(FRAME_PREFIX) :].strip()


def decode_frame_payload(payload: str) -> dict[str, Any] | None:
    """Parse a frame payload into its record, or report that it is malformed.

    Never raises. A malformed frame must not cost the caller the run, so both
    failure shapes return ``None``: a payload that is not JSON, and one that is
    valid JSON but not an object. The second is the shape a reader forgets —
    ``json.loads`` accepts ``"a string"``, ``7`` and ``[1, 2]`` quite happily,
    and each would otherwise be appended to the caller's diagnostics as though
    it were a record.

    Args:
        payload: The text following the marker.

    Returns:
        The decoded record, or ``None`` when it is not one.
    """
    try:
        parsed = json.loads(payload)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def emit_diagnostic(entry: dict[str, Any], *, stream: TextIO | None = None) -> None:
    try:
        target = stream or sys.stderr
        target.write(encode_frame(entry) + "\n")
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
        entry = apply_line_limit(entry)
        self.entries.append(entry)
        emit_diagnostic(entry, stream=self.stream)
