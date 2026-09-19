"""Live check that SerpBase answers the request ``search_serpbase`` actually sends.

**This is the test that closes the open risk in the SerpBase provider.** Nothing
else in the suite can. ``test_serpbase_unit.py`` proves what is *sent* and what
the parser does with a body it is *handed*, but both halves are asserted against
a mock written from SerpBase's documentation in the same change that rewrote the
provider. That is self-consistency, not evidence: if the documented contract is
wrong, or changes again, the unit tests keep passing while every real call fails.

The contract at stake is the whole of it, because the provider moved from
``GET`` with an ``api_key`` query parameter to ``POST`` with an ``X-API-Key``
header, a JSON body and an ``organic`` response key -- method, authentication,
request encoding and response envelope all at once. A discrepancy in any one of
them is invisible offline.

**A skipped live test proves nothing**, and this one skips by default. It becomes
evidence only when somebody with a key runs it::

    KINDLY_RUN_LIVE_TESTS=1 SERPBASE_API_KEY=... pytest tests/test_serpbase_live.py

Gated on ``KINDLY_RUN_LIVE_TESTS`` and marked ``live``, per section 6.3 -- the
target gate, rather than the older ``RUN_LIVE_TESTS`` that ``test_serper_live.py``
still reads.

The assertions are deliberately structural, not exact: a real SERP changes
between runs, so this checks the envelope and the field *types*
``search_serpbase`` depends on, never the content of a particular result. The one
exception is the HTTP method and the header name, which are the parts the
rewrite actually changed and the parts a silent revert would undo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.search.serpbase import SEARCH_ENDPOINT, search_serpbase

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
    """Return the SerpBase key, skipping or failing rather than passing quietly.

    A missing key when live tests are *off* is a skip. A missing key when they
    are *on* is a failure: section 6.3's rule is that a skipped live suite is a
    failed live suite, so an enabled job that silently skipped would report green
    while proving nothing.

    Returns:
        The key to authenticate with.
    """
    if not _live_enabled():
        pytest.skip("Live tests disabled; set KINDLY_RUN_LIVE_TESTS=1 to enable")

    key = os.environ.get("SERPBASE_API_KEY", "").strip()
    assert key, "KINDLY_RUN_LIVE_TESTS is set but SERPBASE_API_KEY is missing"
    return key


async def test_the_documented_request_is_the_one_serpbase_accepts(
    live_key: str,
) -> None:
    """Confirm the method, the header, the body and the response envelope hold.

    Sends the exact request ``search_serpbase`` builds -- same endpoint, same
    header, same JSON body -- and reads the response the way the provider's
    parser does. A 405 says it is not POST-only after all; a 401 with a valid key
    says the header name is wrong; a body with no ``organic`` key says the
    envelope moved. Each of those is the evidence the unit tests cannot supply.

    Args:
        live_key: The SerpBase key, from the gating fixture.
    """
    payload = {"q": "model context protocol"}
    headers = {"X-API-Key": live_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SEARCH_ENDPOINT, headers=headers, json=payload)

    assert response.status_code != 401, (
        "SerpBase rejected the key sent in an `X-API-Key` header. Either the key "
        "is invalid -- in which case this run proves nothing -- or the header "
        "name has changed and `serpbase.py` needs to follow it"
    )
    assert response.status_code != 405, (
        "SerpBase answered 405 to a POST, so the endpoint is no longer POST-only "
        "and the provider's method is wrong"
    )
    assert response.status_code == 200, (
        f"SerpBase answered HTTP {response.status_code} for the documented "
        "request; the endpoint, the method or the body shape is wrong"
    )

    body = response.json()
    assert isinstance(body, dict), "response body is not a JSON object"

    organic = body.get("organic")
    assert isinstance(organic, list), (
        "`organic` is missing or is not a list. The provider reads results from "
        "this key, and it was `organic_results` before the API changed -- a move "
        "back would be silent offline"
    )
    assert organic, "a live query returned no organic results at all"

    first = organic[0]
    assert isinstance(first, dict), "an organic entry is not an object"
    # The parser keeps only entries carrying all three as strings, so a type
    # change in any of them empties the result list rather than erroring.
    for field in ("title", "link", "snippet"):
        assert isinstance(first.get(field), str), f"`{field}` is not a string"


async def test_the_provider_returns_usable_results_end_to_end(
    live_key: str,
) -> None:
    """Drive ``search_serpbase`` itself so the parser meets the real payload.

    The request test above could pass while the parser still discarded every
    live entry -- it keeps only entries whose ``title``, ``link`` and ``snippet``
    are all strings, and drops the rest *silently*. So "the envelope is right"
    and "the parser returns something" are two claims, and this is the second.

    Args:
        live_key: The SerpBase key, from the gating fixture.
    """
    results = await search_serpbase("model context protocol", num_results=3)

    assert results, (
        "SerpBase answered but the parser kept nothing. The envelope matched and "
        "the entries did not: check the field names inside `organic`"
    )
    assert len(results) <= 3, "the provider returned more than it was asked for"

    first = results[0]
    assert first.title and isinstance(first.title, str)
    assert first.link and isinstance(first.link, str)
    assert isinstance(first.snippet, str)
