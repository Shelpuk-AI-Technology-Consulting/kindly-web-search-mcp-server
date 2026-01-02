from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """
    Configure logging defaults for both local runs and MCP stdio hosts.

    Goals:
    - Avoid noisy third-party logs during tool execution (especially `httpx` request logs).
    - Keep configuration idempotent so hosts can override it safely.
    """
    root = logging.getLogger()

    # Only set up basicConfig if nothing configured yet (common for scripts).
    if not root.handlers:
        level = os.environ.get("LOG_LEVEL", "WARNING").upper()
        logging.basicConfig(level=getattr(logging, level, logging.WARNING))

    # Silence common noisy libraries unless the host explicitly configures them.
    noisy_loggers = (
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "nodriver",
        "undetected_chromedriver",
    )
    for name in noisy_loggers:
        # `asyncio` can emit noisy warnings about slow callbacks in some environments.
        level = logging.ERROR if name == "asyncio" else logging.WARNING
        logging.getLogger(name).setLevel(level)
