from __future__ import annotations

import sys
import os
from pathlib import Path
import unittest
import asyncio
from unittest.mock import AsyncMock, patch
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.models import WebSearchResult

# The one knob the concurrency resolver reads. Named once so the case table,
# the environment each case installs and the failure message cannot disagree.
ENVIRONMENT_NAME = "KINDLY_WEB_SEARCH_MAX_CONCURRENCY"


class ConcurrencyCase(NamedTuple):
    """One row of the web-search concurrency resolver's case table.

    Attributes:
        label: The behaviour the row pins. It names the ``subTest``, so a
            failure reads as the claim that broke rather than as an index.
        env: The ``KINDLY_WEB_SEARCH_MAX_CONCURRENCY`` value for the row, or
            ``None`` to leave the variable unset.
        num_results: The result count handed to the resolver.
        expected: The concurrency the resolver must return.
        pins: The single-line mutation of the resolver that this row detects.
            Carried on the row rather than in prose so it cannot drift from the
            case it describes, and reported on failure so the reader is told
            which production line the row was watching.
    """

    label: str
    env: str | None
    num_results: int
    expected: int
    pins: str


# The resolver's whole contract, one row per behaviour. ``num_results`` is chosen
# per row so that no bound masks another: the ceiling row would read as a pass
# under a small result count, which is exactly how the ``1..5`` ceiling came to
# have no coverage at all.
WEB_SEARCH_CONCURRENCY_CASES: tuple[ConcurrencyCase, ...] = (
    ConcurrencyCase(
        "unset falls back to the default", None, 5, 3, "change the `value = 3` default"
    ),
    ConcurrencyCase(
        "an explicit value is honoured", "2", 5, 2, "ignore the variable entirely"
    ),
    ConcurrencyCase(
        "a value above the ceiling is clamped", "10", 8, 5, "delete `min(value, 5)`"
    ),
    ConcurrencyCase(
        "an unparseable value falls back",
        "abc",
        5,
        3,
        "delete the `except ValueError` handler",
    ),
    ConcurrencyCase(
        "zero falls back",
        "0",
        5,
        3,
        "`if parsed and parsed > 0` -> `if parsed is not None`",
    ),
    ConcurrencyCase(
        "a negative value falls back",
        "-2",
        5,
        3,
        "drop the `> 0` conjunct, leaving `if parsed`",
    ),
    ConcurrencyCase(
        "the result count bounds the value",
        "5",
        2,
        2,
        "delete `min(value, num_results)`",
    ),
)


class TestWebSearchTool(unittest.IsolatedAsyncioTestCase):
    def test_tool_timeout_budget_can_exceed_55_seconds(self) -> None:
        from kindly_web_search_mcp_server.server import _resolve_tool_total_timeout_seconds

        with patch.dict(
            os.environ,
            {
                "KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS": "120",
                "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": "600",
            },
            clear=False,
        ):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 120.0)

        with patch.dict(
            os.environ,
            {
                "KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS": "120",
                "KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": "100",
            },
            clear=False,
        ):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 100.0)

        with patch.dict(
            os.environ,
            {"KINDLY_TOOL_TOTAL_TIMEOUT_SECONDS": "abc"},
            clear=False,
        ):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 120.0)

        with patch.dict(
            os.environ,
            {"KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": "abc"},
            clear=False,
        ):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 120.0)

        with patch.dict(
            os.environ,
            {"KINDLY_TOOL_TOTAL_TIMEOUT_MAX_SECONDS": "90"},
            clear=False,
        ):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 90.0)

    def test_web_search_concurrency_resolves_from_environment_and_result_count(
        self,
    ) -> None:
        """Resolve concurrency from one variable and one result count, on any OS

        Replaces three methods that patched ``server.os.name`` and split their
        expectations by the patched value. ``_resolve_web_search_max_concurrency``
        has no platform branch -- it reads ``KINDLY_WEB_SEARCH_MAX_CONCURRENCY``
        and ``num_results`` and nothing else -- so that split asserted a Windows
        cap the product no longer has, and four of its five cases were wrong.

        Every row of :data:`WEB_SEARCH_CONCURRENCY_CASES` dies to the mutation
        its ``pins`` field names, measured under both a stripped and a hostile
        environment. The positivity guard needs *two* mutations because neither
        alone kills both of its rows: ``0`` is already falsy, so dropping the
        ``> 0`` conjunct leaves the zero row passing while killing the negative
        one.

        ``max(1, ...)`` is not covered here and cannot be. By the time it runs,
        ``value`` is either a parsed integer already filtered to ``> 0`` or the
        literal default, so no input distinguishes it from the identity -- an
        equivalent mutant. It is not inert, though: it is what turns the
        *mutated* zero and negative results into ``1`` rather than ``0`` and
        ``-2``. Either way they die, since the rows expect ``3``; those two
        expect ``3`` because the positivity filter sends them to the fallback,
        which is a different line entirely.

        The cases are driven through ``subTest`` rather than through
        ``pytest.mark.parametrize`` -- which does not apply to a
        :class:`unittest.IsolatedAsyncioTestCase` method anyway -- and the
        consequence is load-bearing beyond this file: seven cases stay on **one**
        node id, which is what lets the ledger's relocation rows name this
        replacement exactly rather than by prefix.

        One claim is deliberately lost. The three deleted methods were, between
        them, the only thing in the tree that would notice an ``os.name`` branch
        reappearing in this resolver; nothing asserts its absence now. The next
        thing to cover it is the suite-green milestone's one real Windows run.

        ``clear=True`` is load-bearing rather than tidy: the assertions read a
        variable a developer's shell may well export, and a case that passes only
        on a machine that does not export it is not a control.
        """
        from kindly_web_search_mcp_server.server import _resolve_web_search_max_concurrency

        for case in WEB_SEARCH_CONCURRENCY_CASES:
            # A cleared environment per row, so an ambient value cannot satisfy a
            # case the resolver would have failed.
            environment = {} if case.env is None else {ENVIRONMENT_NAME: case.env}
            with self.subTest(case.label), patch.dict(
                os.environ, environment, clear=True
            ):
                self.assertEqual(
                    _resolve_web_search_max_concurrency(case.num_results),
                    case.expected,
                    f"{case.label}: {ENVIRONMENT_NAME}="
                    f"{'unset' if case.env is None else repr(case.env)} with "
                    f"num_results={case.num_results}. This row exists to detect "
                    f"'{case.pins}'.",
                )

    def test_tool_timeout_defaults_to_120_seconds(self) -> None:
        from kindly_web_search_mcp_server.server import _resolve_tool_total_timeout_seconds

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_resolve_tool_total_timeout_seconds(), 120.0)

    async def test_web_search_returns_results(self) -> None:
        from kindly_web_search_mcp_server.server import web_search

        mocked_results = [
            WebSearchResult(title="T", link="https://example.com", snippet="S", page_content="")
        ]

        with patch(
            "kindly_web_search_mcp_server.server.search_web", new_callable=AsyncMock
        ) as mock_search, patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_search.return_value = mocked_results
            mock_resolve.return_value = "# Title\n\nHello"

            out = await web_search("hello", num_results=1)

        self.assertIsInstance(out, dict)
        self.assertIn("results", out)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["title"], "T")
        self.assertEqual(out["results"][0]["link"], "https://example.com")
        self.assertEqual(out["results"][0]["snippet"], "S")
        self.assertIn("page_content", out["results"][0])
        self.assertIn("Hello", out["results"][0]["page_content"])

    async def test_get_content_returns_markdown(self) -> None:
        from kindly_web_search_mcp_server.server import get_content

        with patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.return_value = "# Title\n\nHello"
            out = await get_content("https://example.com")

        self.assertEqual(out["url"], "https://example.com")
        self.assertIn("page_content", out)
        self.assertIn("Hello", out["page_content"])

    async def test_get_content_handles_none(self) -> None:
        from kindly_web_search_mcp_server.server import get_content

        with patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.return_value = None
            out = await get_content("https://example.com/file.pdf")

        self.assertEqual(out["url"], "https://example.com/file.pdf")
        self.assertIn("Could not retrieve content", out["page_content"])

    async def test_get_content_returns_timeout_note_on_timeout(self) -> None:
        from kindly_web_search_mcp_server.server import get_content

        with patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_resolve.side_effect = asyncio.TimeoutError()
            out = await get_content("https://example.com")

        self.assertIn("TimeoutError", out["page_content"])
        self.assertIn("Source: https://example.com", out["page_content"])

    async def test_web_search_returns_timeout_note_on_timeout(self) -> None:
        from kindly_web_search_mcp_server.server import web_search

        mocked_results = [
            WebSearchResult(
                title="T",
                link="https://example.com",
                snippet="S",
                page_content="",
            )
        ]

        with patch(
            "kindly_web_search_mcp_server.server.search_web", new_callable=AsyncMock
        ) as mock_search, patch(
            "kindly_web_search_mcp_server.server.resolve_page_content_markdown",
            new_callable=AsyncMock,
        ) as mock_resolve:
            mock_search.return_value = mocked_results
            mock_resolve.side_effect = asyncio.TimeoutError()
            out = await web_search("hello", num_results=1)

        self.assertIn("TimeoutError", out["results"][0]["page_content"])
        self.assertIn("Source: https://example.com", out["results"][0]["page_content"])


if __name__ == "__main__":
    unittest.main()
