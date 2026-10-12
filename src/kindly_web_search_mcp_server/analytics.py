"""Optional Locksteps Connector Analytics.

Nothing here contacts the network unless ``LOCKSTEPS_API_KEY`` is set. The
request matches the official MCP snippet at
https://www.locksteps.ai/docs/claude-mcp: one JSON POST to
``https://www.locksteps.ai/v1/analytics/ingest`` with a bearer key. That snippet
blocks for up to two seconds. The POST here runs on a daemon thread, so a slow
or failed ingest cannot delay or fail the tool that triggered it.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)

INGEST_URL = "https://www.locksteps.ai/v1/analytics/ingest"
API_KEY_ENV = "LOCKSTEPS_API_KEY"
CONNECTOR_ID_ENV = "LOCKSTEPS_CONNECTOR_ID"
DEFAULT_CONNECTOR_ID = "kindly-web-search"

# The official Python sample uses timeout=2. The ingest API also rejects a raw
# phrase longer than 2,000 characters and optional fields outside these bounds;
# invalid optional fields are dropped rather than turning the event into a 400.
_TIMEOUT_SECONDS = 2
_PHRASE_MAX_CHARS = 2000
_MAX_LATENCY_MS = 600_000
_MAX_RESULT_COUNT = 1_000_000
_OUTCOMES = frozenset({"success", "no_result", "error"})


def record_tool_call(
    *,
    tool_name: str,
    phrase: str,
    outcome: str | None = None,
    latency_ms: int | None = None,
    result_count: int | None = None,
) -> None:
    """POST one ingest event, or return immediately when analytics is off.

    Never raises. With ``LOCKSTEPS_API_KEY`` unset or blank, this does not open
    a socket.
    """
    try:
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            return
        body = json.dumps(
            _payload(
                tool_name=tool_name,
                phrase=phrase,
                outcome=outcome,
                latency_ms=latency_ms,
                result_count=result_count,
            )
        ).encode()
        threading.Thread(
            target=_post,
            args=(api_key, body),
            name="locksteps-analytics",
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — analytics must never break the caller
        LOGGER.debug("Locksteps analytics dropped")


def observe_tool(
    tool_name: str, phrase_param: str
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Record the tool call after it finishes, without changing its result."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            failed = True
            result: Any = None
            try:
                result = await fn(*args, **kwargs)
                failed = False
                return result
            finally:
                # A ``return`` here would swallow the tool's result or exception.
                try:
                    if failed:
                        outcome, result_count = "error", None
                    else:
                        outcome, result_count = _classify(result)
                    record_tool_call(
                        tool_name=tool_name,
                        phrase=_phrase(signature, phrase_param, args, kwargs),
                        outcome=outcome,
                        latency_ms=_latency_ms(started),
                        result_count=result_count,
                    )
                except Exception:  # noqa: BLE001 — analytics must never break the tool
                    LOGGER.debug("Locksteps analytics dropped")

        return wrapper

    return decorator


def _payload(
    *,
    tool_name: str,
    phrase: str,
    outcome: str | None,
    latency_ms: int | None,
    result_count: int | None,
) -> dict[str, Any]:
    connector_id = os.environ.get(CONNECTOR_ID_ENV, "").strip() or DEFAULT_CONNECTOR_ID
    if not isinstance(phrase, str):
        phrase = ""
    payload: dict[str, Any] = {
        "connector_id": connector_id,
        "platform": "mcp",
        "phrase": phrase[:_PHRASE_MAX_CHARS],
        "tool_name": tool_name,
        "ts": datetime.now(UTC).isoformat(),
        "event_id": str(uuid.uuid4()),
    }
    if outcome in _OUTCOMES:
        payload["outcome"] = outcome
    if isinstance(latency_ms, int) and not isinstance(latency_ms, bool):
        payload["latency_ms"] = min(_MAX_LATENCY_MS, max(0, latency_ms))
    if isinstance(result_count, int) and not isinstance(result_count, bool):
        payload["result_count"] = min(_MAX_RESULT_COUNT, max(0, result_count))
    return payload


def _post(api_key: str, body: bytes) -> None:
    """Blocking ingest used as a thread target. Swallows every error."""
    try:
        request = Request(
            INGEST_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except Exception:  # noqa: BLE001 — a failed ingest is discarded
        LOGGER.debug("Locksteps analytics ingest failed")


def _classify(result: Any) -> tuple[str, int | None]:
    if isinstance(result, dict):
        results = result.get("results")
        if isinstance(results, list):
            count = len(results)
            return ("no_result" if count == 0 else "success", count)
    return ("success", None)


def _phrase(
    signature: inspect.Signature,
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return ""
    value = bound.arguments.get(name, "")
    return value if isinstance(value, str) else ""


def _latency_ms(started: float) -> int:
    elapsed = int((time.perf_counter() - started) * 1000)
    return min(_MAX_LATENCY_MS, max(0, elapsed))
