from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """Runtime configuration (env-first).

    Note: keep this module lightweight; it is imported by tests.
    """

    serper_api_key: str = os.environ.get("SERPER_API_KEY", "")


settings = Settings()

