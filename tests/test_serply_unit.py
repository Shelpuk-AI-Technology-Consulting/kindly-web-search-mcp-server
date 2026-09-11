from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestSerplyParsing(unittest.TestCase):
    def test_search_serply_parses_results(self) -> None:
        async def run() -> None:
            os.environ["SERPLY_API_KEY"] = "serply_test"

            from kindly_web_search_mcp_server.search.serply import search_serply

            serply_payload = {
                "results": [
                    {
                        "title": "Async Support - HTTPX",
                        "link": "https://www.python-httpx.org/async/",
                        "description": "HTTPX offers an optional async client.",
                        "position": 1,
                        "realPosition": 1,
                        "result_type": "organic",
                    }
                ],
                "total": 1,
                "query": "httpx",
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.host, "api.serply.io")
                self.assertEqual(request.url.path, "/v1/search")
                self.assertEqual(request.url.params["q"], "httpx")
                self.assertEqual(request.headers.get("x-api-key"), "serply_test")
                return httpx.Response(200, json=serply_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_serply("httpx", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Async Support - HTTPX")
            self.assertEqual(results[0].link, "https://www.python-httpx.org/async/")
            self.assertEqual(results[0].snippet, "HTTPX offers an optional async client.")

        anyio.run(run)


class TestSerplyResults(unittest.TestCase):
    """Cover the cap, the snippet fallback, and the empty and mismatched shapes.

    Serply returns organic results as one ``results`` list whose items carry
    ``title``, ``link`` and ``description``. The router needs the list capped at
    the caller's ``num_results``, and a missing or reshaped list must surface as
    an error rather than as zero hits.
    """

    def setUp(self) -> None:
        """Set a dummy API key and restore the previous value afterwards"""
        previous = os.environ.get("SERPLY_API_KEY")
        os.environ["SERPLY_API_KEY"] = "serply_test"

        def restore() -> None:
            if previous is None:
                os.environ.pop("SERPLY_API_KEY", None)
            else:
                os.environ["SERPLY_API_KEY"] = previous

        self.addCleanup(restore)

    def _search(
        self,
        payload: dict[str, Any],
        *,
        num_results: int = 3,
        sent: dict[str, Any] | None = None,
    ) -> Any:
        """Run ``search_serply`` against a mocked response.

        Args:
            payload: JSON body the mocked Serply API returns.
            num_results: Value forwarded to ``search_serply``.
            sent: When given, receives the request's query parameters.

        Returns:
            The parsed results.
        """

        async def run() -> Any:
            from kindly_web_search_mcp_server.search.serply import search_serply

            def handler(request: httpx.Request) -> httpx.Response:
                if sent is not None:
                    sent.update(dict(request.url.params))
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await search_serply("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_caps_results_at_num_results(self) -> None:
        """Bound the list locally rather than trusting the count the API returns"""
        results = self._search(
            {
                "results": [
                    {"title": f"Result {i}", "link": f"https://example.org/{i}", "description": "s"}
                    for i in range(3)
                ]
            },
            num_results=2,
        )

        self.assertEqual(len(results), 2)

    def test_keeps_result_that_has_no_description(self) -> None:
        """Keep a usable link even with no snippet, since page_content is fetched later"""
        results = self._search({"results": [{"title": "Result", "link": "https://example.org"}]})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].snippet, "")

    def test_ignores_a_description_that_is_not_a_string(self) -> None:
        """Treat a malformed `description` as absent rather than storing it"""
        results = self._search(
            {"results": [{"title": "Result", "link": "https://example.org", "description": None}]}
        )

        self.assertEqual(results[0].snippet, "")

    def test_skips_an_entry_whose_title_is_not_a_string(self) -> None:
        """Drop a result with a usable link but no usable title

        A good ``link`` beside the bad title is what makes the ``title`` conjunct
        observable; in the all-unusable payload below every bad-title entry also
        has a bad ``link``, so the ``link`` conjunct rejects it first.
        """
        results = self._search(
            {
                "results": [
                    {"title": 7, "link": "https://odd-title.example/", "description": "s"},
                    {"title": "Good", "link": "https://good.example/", "description": "ok"},
                ]
            }
        )

        self.assertEqual([result.title for result in results], ["Good"])

    def test_raises_when_no_returned_result_is_usable(self) -> None:
        """Fail loudly instead of returning nothing when the schema does not match"""
        from kindly_web_search_mcp_server.search.serply import SerplyError

        with self.assertRaises(SerplyError) as caught:
            self._search(
                {
                    "results": [
                        {"headline": "no title or link"},
                        "not an object",
                        {"title": "Result", "link": 123},
                    ]
                }
            )

        self.assertIn("3", str(caught.exception))

    def test_returns_empty_when_the_api_found_nothing(self) -> None:
        """Return no results, without error, when the query genuinely matched nothing"""
        self.assertEqual(self._search({"results": [], "total": 0}), [])

    def test_raises_when_results_list_is_missing(self) -> None:
        """A response without a `results` list is a schema change, not zero hits"""
        from kindly_web_search_mcp_server.search.serply import SerplyError

        with self.assertRaises(SerplyError):
            self._search({"query": "q", "total": 0})

    def test_raises_when_results_is_not_a_list(self) -> None:
        """Reject a `results` value of the wrong type instead of iterating it"""
        from kindly_web_search_mcp_server.search.serply import SerplyError

        with self.assertRaises(SerplyError):
            self._search({"results": {"title": "Result", "link": "https://example.org"}})

    def test_sends_query_and_num_as_query_parameters(self) -> None:
        """Forward `q` and `num` in the query string, as the API documents"""
        sent: dict[str, Any] = {}
        self._search({"results": []}, sent=sent)

        self.assertEqual(sent["q"], "q")
        self.assertEqual(sent["num"], "3")

    def test_missing_key_raises_config_error(self) -> None:
        """A missing SERPLY_API_KEY is a provider configuration failure"""
        from kindly_web_search_mcp_server.search.serply import SerplyConfigError

        previous = os.environ.get("SERPLY_API_KEY")
        os.environ.pop("SERPLY_API_KEY", None)

        def restore() -> None:
            if previous is None:
                os.environ.pop("SERPLY_API_KEY", None)
            else:
                os.environ["SERPLY_API_KEY"] = previous

        self.addCleanup(restore)

        async def run() -> None:
            from kindly_web_search_mcp_server.search.serply import search_serply

            with self.assertRaises(SerplyConfigError):
                await search_serply("q", num_results=1)

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
