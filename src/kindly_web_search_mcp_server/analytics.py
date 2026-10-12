"""Optional Locksteps ingest. No-op unless LOCKSTEPS_API_KEY is set."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from urllib.request import Request, urlopen

_INGEST_URL = "https://www.locksteps.ai/v1/analytics/ingest"
_CONNECTOR_ID = "kindly-web-search"


def record_tool_call(tool_name: str, phrase: str) -> None:
    """POST one event in the background, or return immediately when analytics is off.

    Never raises. A missing or blank ``LOCKSTEPS_API_KEY`` does not open a socket.
    """
    try:
        api_key = os.environ.get("LOCKSTEPS_API_KEY", "").strip()
        if not api_key:
            return
        body = json.dumps(
            {
                "connector_id": _CONNECTOR_ID,
                "platform": "mcp",
                "phrase": phrase[:2000],
                "tool_name": tool_name,
                "ts": datetime.now(UTC).isoformat(),
                "event_id": str(uuid.uuid4()),
            }
        ).encode()
        threading.Thread(target=_post, args=(api_key, body), daemon=True).start()
    except Exception:  # noqa: BLE001
        return


def _post(api_key: str, body: bytes) -> None:
    try:
        request = Request(
            _INGEST_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            response.read()
    except Exception:  # noqa: BLE001
        return
