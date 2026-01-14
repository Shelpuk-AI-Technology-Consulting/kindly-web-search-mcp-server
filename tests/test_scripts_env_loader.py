from __future__ import annotations

import sys
from pathlib import Path


def test_redact_env_value_redacts_proxy() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts._env_loader import redact_env_value

    assert redact_env_value("HTTP_PROXY", "http://user:pass@proxy:8080") == "***REDACTED***"
    assert redact_env_value("no_proxy", "localhost,127.0.0.1") == "***REDACTED***"
    assert redact_env_value("ALL_PROXY", "socks5://proxy:1080") == "***REDACTED***"


def test_redact_env_value_redacts_secrets() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts._env_loader import redact_env_value

    assert redact_env_value("SERPER_API_KEY", "abc") == "***REDACTED***"
    assert redact_env_value("GITHUB_TOKEN", "abc") == "***REDACTED***"
    assert redact_env_value("PASSWORD", "abc") == "***REDACTED***"


def test_redact_env_value_allows_non_secrets() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts._env_loader import redact_env_value

    assert redact_env_value("LOG_LEVEL", "INFO") == "INFO"
