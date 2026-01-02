import pytest
from kindly_web_search_mcp_server import settings


@pytest.fixture(scope="session", autouse=True)
def patch_settings():
    """
    Patch settings for the test session.
    """
    # Only set a dummy key if one is not provided by the environment/tests.
    # This keeps unit tests deterministic while allowing opt-in live integration tests.
    if not settings.settings.serper_api_key:
        settings.settings.serper_api_key = "test_api_key"
