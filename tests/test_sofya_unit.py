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


class TestSofyaParsing(unittest.TestCase):
    def test_search_sofya_parses_results(self) -> None:
        async def run() -> None:
            os.environ["SOFYA_API_KEY"] = "sofya_test"

            from kindly_web_search_mcp_server.search.sofya import search_sofya

            sofya_payload = {
                "query": "leo messi",
                "results": [
                    {
                        "title": "Lionel Messi Facts | Britannica",
                        "url": "https://www.britannica.com/facts/Lionel-Messi",
                        "content": "Lionel Messi, an Argentine footballer...",
                    }
                ],
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), "https://sofya.co/v1/search")
                self.assertEqual(request.headers.get("authorization"), "Bearer sofya_test")
                return httpx.Response(200, json=sofya_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_sofya("leo messi", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Lionel Messi Facts | Britannica")
            self.assertEqual(results[0].link, "https://www.britannica.com/facts/Lionel-Messi")
            self.assertTrue(results[0].snippet)

        anyio.run(run)


class TestSofyaSnippetSource(unittest.TestCase):
    """Cover which response field supplies the snippet, and the empty-result path.

    Sofya returns extracted page text in ``content`` only when it fetched the page.
    This client requests ``search_depth="snippets"``, which explicitly does not
    fetch pages, so the SERP text arrives in ``description`` instead. Reading only
    ``content`` therefore risks discarding every result.
    """

    def setUp(self) -> None:
        """Set a dummy API key and restore the previous value afterwards"""
        previous = os.environ.get("SOFYA_API_KEY")
        os.environ["SOFYA_API_KEY"] = "sofya_test"

        def restore() -> None:
            if previous is None:
                os.environ.pop("SOFYA_API_KEY", None)
            else:
                os.environ["SOFYA_API_KEY"] = previous

        self.addCleanup(restore)

    def _search(
        self,
        payload: dict[str, Any],
        *,
        num_results: int = 3,
        sent: dict[str, Any] | None = None,
    ) -> Any:
        """Run ``search_sofya`` against a mocked response.

        Args:
            payload: JSON body the mocked Sofya API returns.
            num_results: Value forwarded to ``search_sofya``.
            sent: When given, receives the decoded request body.

        Returns:
            The parsed results.
        """

        async def run() -> Any:
            from kindly_web_search_mcp_server.search.sofya import search_sofya

            def handler(request: httpx.Request) -> httpx.Response:
                if sent is not None:
                    sent.update(json.loads(request.content))
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await search_sofya("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_uses_description_when_content_is_absent(self) -> None:
        """Read the SERP snippet, which is what `snippets` depth actually returns"""
        results = self._search(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.org",
                        "description": "SERP snippet text",
                        "fetched": False,
                    }
                ]
            }
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "SERP snippet text")

    def test_uses_description_when_content_is_null(self) -> None:
        """Treat an explicit null `content` the same as an absent one"""
        results = self._search(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.org",
                        "content": None,
                        "description": "SERP snippet text",
                    }
                ]
            }
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "SERP snippet text")

    def test_uses_description_when_content_is_an_empty_string(self) -> None:
        """Treat a blank `content` as absent rather than as the snippet"""
        results = self._search(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.org",
                        "content": "",
                        "description": "SERP snippet text",
                    }
                ]
            }
        )

        self.assertEqual(results[0].snippet, "SERP snippet text")

    def test_uses_description_when_content_is_not_a_string(self) -> None:
        """Fall through to `description` when `content` is a non-string value

        A truthiness-only fallback latches onto the non-string and never reaches
        `description`, silently returning an empty snippet.
        """
        for odd_content in ({"nested": "object"}, 123, ["list"]):
            with self.subTest(content=odd_content):
                results = self._search(
                    {
                        "results": [
                            {
                                "title": "Result",
                                "url": "https://example.org",
                                "content": odd_content,
                                "description": "SERP snippet text",
                            }
                        ]
                    }
                )

                self.assertEqual(results[0].snippet, "SERP snippet text")

    def test_prefers_content_when_both_are_present(self) -> None:
        """Keep extracted page text when the API did fetch the page"""
        results = self._search(
            {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.org",
                        "content": "Extracted page content",
                        "description": "SERP snippet text",
                    }
                ]
            }
        )

        self.assertEqual(results[0].snippet, "Extracted page content")

    def test_keeps_result_that_has_no_snippet_text(self) -> None:
        """Keep a usable link even with no snippet, since page_content is fetched later"""
        results = self._search(
            {"results": [{"title": "Result", "url": "https://example.org"}]}
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "")

    def test_raises_when_no_returned_result_is_usable(self) -> None:
        """Fail loudly instead of returning nothing when the schema does not match

        Covers both ways a result is discarded: an entry that is not an object,
        and an object without a usable string ``title`` and ``url``.
        """
        from kindly_web_search_mcp_server.search.sofya import SofyaError

        with self.assertRaises(SofyaError) as caught:
            self._search(
                {
                    "results": [
                        {"headline": "no title or url"},
                        "not an object",
                        {"title": "Result", "url": 123},
                    ]
                }
            )

        self.assertIn("3", str(caught.exception))

    def test_returns_empty_when_the_api_found_nothing(self) -> None:
        """Return no results, without error, when the query genuinely matched nothing"""
        self.assertEqual(self._search({"results": []}), [])

    def test_clamps_max_results_to_the_documented_maximum(self) -> None:
        """Clamp `max_results` to 20, the documented ceiling, instead of sending 400"""
        sent: dict[str, Any] = {}
        self._search({"results": []}, num_results=50, sent=sent)

        self.assertEqual(sent["max_results"], 20)

    def test_requests_snippet_depth(self) -> None:
        """Request the cheap depth, since page content is resolved by this server"""
        sent: dict[str, Any] = {}
        self._search({"results": []}, sent=sent)

        self.assertEqual(sent["search_depth"], "snippets")


if __name__ == "__main__":
    unittest.main()
