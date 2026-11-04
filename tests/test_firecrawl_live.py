"""Live check that Firecrawl answers the request ``search_firecrawl`` sends.

``test_firecrawl_unit.py`` proves what is *sent* and what the parser does with a
body it is *handed*. This module is the other half: it sends that request to the
real API and checks the envelope the parser depends on.

**A skipped live test proves nothing**, and this one skips by default. It becomes
evidence only when someone with a key runs it:

    KINDLY_RUN_LIVE_TESTS=1 FIRECRAWL_API_KEY=... pytest tests/test_firecrawl_live.py

Gated on ``KINDLY_RUN_LIVE_TESTS`` and marked ``live``, per section 6.3, like
``test_apifare_live.py``.

The assertions are deliberately structural, not exact: a real result page
changes between runs, so this checks the envelope and the field *types*
``search_firecrawl`` depends on, never the content of a particular result.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.search.firecrawl import (
    ORIGIN,
    SEARCH_ENDPOINT,
    search_firecrawl,
)

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    """Report whether live tests are switched on for this run.

    Returns:
        ``True`` when ``KINDLY_RUN_LIVE_TESTS`` holds an affirmative value.
    """
    return os.environ.get("KINDLY_RUN_LIVE_TESTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


@pytest.fixture
def live_key() -> str:
    """Return the Firecrawl key, skipping or failing rather than passing quietly.

    A missing key when live tests are *off* is a skip. A missing key when they
    are *on* is a failure: section 6.3's rule is that a skipped live suite is a
    failed live suite.

    Returns:
        The key to authenticate with.
    """
    if not _live_enabled():
        pytest.skip("Live tests disabled; set KINDLY_RUN_LIVE_TESTS=1 to enable")

    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    assert key, "KINDLY_RUN_LIVE_TESTS is set but FIRECRAWL_API_KEY is missing"
    return key


async def test_the_documented_route_answers_the_request_we_send(live_key: str) -> None:
    """Confirm the route, the request body and the response envelope all hold

    Sends the request ``search_firecrawl`` builds (same URL, same header, same
    body) and reads the response the way the provider's parser does.

    Args:
        live_key: The Firecrawl key, from the gating fixture.
    """
    payload = {"query": "model context protocol", "limit": 3, "origin": ORIGIN}
    headers = {"Authorization": f"Bearer {live_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SEARCH_ENDPOINT, headers=headers, json=payload)

    assert response.status_code == 200, (
        f"Firecrawl answered HTTP {response.status_code} for the documented route"
    )

    body = response.json()
    assert isinstance(body, dict), "response body is not a JSON object"
    assert isinstance(body.get("data"), dict), "response has no `data` object"

    items = body["data"].get("web")
    assert isinstance(items, list), "`data.web` is not a list"
    assert items, "a live query returned no results at all"

    first = items[0]
    assert isinstance(first, dict), "a result entry is not an object"
    assert isinstance(first.get("title"), str) and first["title"]
    assert isinstance(first.get("url"), str) and first["url"]
    # `description` is optional in the parser, so its absence is not a failure,
    # only a non-string value where one is present would be.
    assert isinstance(first.get("description", ""), str)


async def test_the_provider_returns_usable_results_end_to_end(live_key: str) -> None:
    """Drive ``search_firecrawl`` itself so the parser meets the real payload

    Args:
        live_key: The Firecrawl key, from the gating fixture.
    """
    results = await search_firecrawl("model context protocol", num_results=3)

    assert results, "search_firecrawl parsed no results out of a live response"
    assert len(results) <= 3
    for result in results:
        assert result.title
        assert result.link.startswith("http")
        # Filled in later by the MCP tool, never by the provider.
        assert result.page_content == ""
