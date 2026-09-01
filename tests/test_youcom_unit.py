from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestYoucomParsing(unittest.TestCase):
    def test_search_youcom_parses_web_results(self) -> None:
        async def run() -> None:
            os.environ["YDC_API_KEY"] = "ydc_test"

            from kindly_web_search_mcp_server.search.youcom import search_youcom

            youcom_payload = {
                "results": {
                    "web": [
                        {
                            "url": "https://www.python-httpx.org/async/",
                            "title": "Async Support - HTTPX",
                            "description": "HTTPX offers an optional async client.",
                            "snippets": ["Longer excerpt from the page."],
                            "page_age": "2025-11-15T10:30:00",
                        }
                    ],
                    "news": [],
                },
                "metadata": {"search_uuid": "uuid", "query": "httpx", "latency": 0.34},
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), "https://ydc-index.io/v1/search")
                self.assertEqual(request.headers.get("x-api-key"), "ydc_test")
                return httpx.Response(200, json=youcom_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_youcom("httpx", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Async Support - HTTPX")
            self.assertEqual(results[0].link, "https://www.python-httpx.org/async/")
            self.assertTrue(results[0].snippet)

        anyio.run(run)


class TestYoucomSections(unittest.TestCase):
    """Cover the `web`/`news` merge, snippet selection, and the empty-result path.

    The You.com API returns ``results.web`` and — only when the query has news
    intent — ``results.news``, as separate lists. The router needs one merged
    list capped at the caller's ``num_results``, and the snippet arrives in
    ``description`` with an optional longer ``snippets[0]``.
    """

    def setUp(self) -> None:
        """Set a dummy API key and restore the previous value afterwards"""
        previous = os.environ.get("YDC_API_KEY")
        os.environ["YDC_API_KEY"] = "ydc_test"

        def restore() -> None:
            if previous is None:
                os.environ.pop("YDC_API_KEY", None)
            else:
                os.environ["YDC_API_KEY"] = previous

        self.addCleanup(restore)

    def _search(
        self,
        payload: dict[str, Any],
        *,
        num_results: int = 3,
        sent: dict[str, Any] | None = None,
    ) -> Any:
        """Run ``search_youcom`` against a mocked response.

        Args:
            payload: JSON body the mocked You.com API returns.
            num_results: Value forwarded to ``search_youcom``.
            sent: When given, receives the decoded request body.

        Returns:
            The parsed results.
        """

        async def run() -> Any:
            from kindly_web_search_mcp_server.search.youcom import search_youcom

            def handler(request: httpx.Request) -> httpx.Response:
                if sent is not None:
                    sent.update(json.loads(request.content))
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await search_youcom("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_merges_web_and_news_sections(self) -> None:
        """Fold both sections into one list, web first"""
        results = self._search(
            {
                "results": {
                    "web": [
                        {
                            "title": "Web Result",
                            "url": "https://example.org/web",
                            "description": "web snippet",
                        }
                    ],
                    "news": [
                        {
                            "title": "News Result",
                            "url": "https://example.org/news",
                            "description": "news snippet",
                        }
                    ],
                }
            }
        )

        self.assertEqual(
            [r.title for r in results], ["Web Result", "News Result"]
        )

    def test_caps_merged_sections_at_num_results(self) -> None:
        """Bound the merged list locally: `count` is per-section, not global"""
        results = self._search(
            {
                "results": {
                    "web": [
                        {"title": f"Web {i}", "url": f"https://example.org/{i}", "description": "s"}
                        for i in range(3)
                    ],
                    "news": [
                        {"title": "News 0", "url": "https://example.org/news", "description": "s"}
                    ],
                }
            },
            num_results=2,
        )

        self.assertEqual(len(results), 2)

    def test_uses_description_when_snippets_absent(self) -> None:
        """Read the SERP snippet `description` when no `snippets` list is present"""
        results = self._search(
            {
                "results": {
                    "web": [
                        {"title": "Result", "url": "https://example.org", "description": "SERP text"}
                    ]
                }
            }
        )

        self.assertEqual(results[0].snippet, "SERP text")

    def test_prefers_description_over_snippets_zero(self) -> None:
        """Keep `description` when `snippets[0]` carries no text

        The docs describe `snippets` as a longer excerpt, but `description` is
        the stable SERP snippet, so it stays the primary source.
        """
        results = self._search(
            {
                "results": {
                    "web": [
                        {
                            "title": "Result",
                            "url": "https://example.org",
                            "description": "SERP text",
                            "snippets": [""],
                        }
                    ]
                }
            }
        )

        self.assertEqual(results[0].snippet, "SERP text")

    def test_ignores_non_list_snippets(self) -> None:
        """Treat a malformed `snippets` value as absent rather than indexing it"""
        results = self._search(
            {
                "results": {
                    "web": [
                        {
                            "title": "Result",
                            "url": "https://example.org",
                            "description": "SERP text",
                            "snippets": "not a list",
                        }
                    ]
                }
            }
        )

        self.assertEqual(results[0].snippet, "SERP text")

    def test_keeps_result_that_has_no_snippet_text(self) -> None:
        """Keep a usable link even with no snippet, since page_content is fetched later"""
        results = self._search(
            {
                "results": {
                    "web": [{"title": "Result", "url": "https://example.org"}]
                }
            }
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "")

    def test_raises_when_no_returned_result_is_usable(self) -> None:
        """Fail loudly instead of returning nothing when the schema does not match"""
        from kindly_web_search_mcp_server.search.youcom import YoucomError

        with self.assertRaises(YoucomError) as caught:
            self._search(
                {
                    "results": {
                        "web": [
                            {"headline": "no title or url"},
                            "not an object",
                            {"title": "Result", "url": 123},
                        ]
                    }
                }
            )

        self.assertIn("3", str(caught.exception))

    def test_returns_empty_when_only_unparsed_sections_carry_entries(self) -> None:
        """A zero-hit answer stays a zero-hit answer beside an unread section

        `results` may carry sections this provider does not read -- You.com
        returns `news` only on news intent, and the surface can grow others. The
        schema-mismatch guard must count only the sections actually parsed;
        counting every list turns a legitimate "no web matches" response into a
        hard error the moment an unread section appears next to it.
        """
        results = self._search(
            {
                "results": {
                    "web": [],
                    "images": [{"src": "https://example.org/a.png"}],
                }
            }
        )

        self.assertEqual(results, [])

    def test_raises_when_a_parsed_section_is_entirely_unusable(self) -> None:
        """The mismatch guard still fires for the sections that are read"""
        from kindly_web_search_mcp_server.search.youcom import YoucomError

        with self.assertRaises(YoucomError):
            self._search(
                {
                    "results": {
                        "web": [{"title": "No link"}],
                        "images": [{"src": "https://example.org/a.png"}],
                    }
                }
            )

    def test_clamps_count_to_the_documented_maximum(self) -> None:
        """`count` is documented as at most 100, so never send more

        The sibling provider `sofya.py` clamps for the same reason: the API
        rejects an out-of-range value, which would turn a large `num_results`
        into a failed search rather than a smaller one.
        """
        sent: dict[str, Any] = {}
        self._search({"results": {"web": []}}, num_results=500, sent=sent)

        self.assertEqual(sent["count"], 100)

    def test_returns_empty_when_the_api_found_nothing(self) -> None:
        """Return no results, without error, when the query genuinely matched nothing"""
        self.assertEqual(self._search({"results": {"web": [], "news": []}}), [])

    def test_raises_when_results_object_is_missing(self) -> None:
        """A response without a `results` object is a schema change, not zero hits"""
        from kindly_web_search_mcp_server.search.youcom import YoucomError

        with self.assertRaises(YoucomError):
            self._search({"metadata": {"query": "q"}})

    def test_sends_count_with_the_query(self) -> None:
        """Forward `count` as the per-section maximum the API documents"""
        sent: dict[str, Any] = {}
        self._search({"results": {"web": []}}, sent=sent)

        self.assertEqual(sent["query"], "q")
        self.assertEqual(sent["count"], 3)

    def test_missing_key_raises_config_error(self) -> None:
        """A missing YDC_API_KEY is a provider configuration failure"""
        from kindly_web_search_mcp_server.search.youcom import YoucomConfigError

        previous = os.environ.get("YDC_API_KEY")
        os.environ.pop("YDC_API_KEY", None)

        def restore() -> None:
            if previous is None:
                os.environ.pop("YDC_API_KEY", None)
            else:
                os.environ["YDC_API_KEY"] = previous

        self.addCleanup(restore)

        async def run() -> None:
            from kindly_web_search_mcp_server.search.youcom import search_youcom

            with self.assertRaises(YoucomConfigError):
                await search_youcom("q", num_results=1)

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
