from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestTavilyParsing(unittest.TestCase):
    def test_search_tavily_parses_results(self) -> None:
        async def run() -> None:
            os.environ["TAVILY_API_KEY"] = "tvly_test"

            from kindly_web_search_mcp_server.search.tavily import search_tavily

            tavily_payload = {
                "query": "leo messi",
                "results": [
                    {
                        "title": "Lionel Messi Facts | Britannica",
                        "url": "https://www.britannica.com/facts/Lionel-Messi",
                        "content": "Lionel Messi, an Argentine footballer...",
                        "score": 0.81,
                    }
                ],
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), "https://api.tavily.com/search")
                self.assertEqual(request.headers.get("authorization"), "Bearer tvly_test")
                return httpx.Response(200, json=tavily_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_tavily("leo messi", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Lionel Messi Facts | Britannica")
            self.assertEqual(results[0].link, "https://www.britannica.com/facts/Lionel-Messi")
            self.assertTrue(results[0].snippet)

        anyio.run(run)


class TestTavilyItemParsing(unittest.TestCase):
    """Cover the item loop and the one place Tavily differs from Serper.

    A ``results`` value that is present but not a list raises ``TavilyError``
    here, where ``serper.py`` returns an empty list for the same shape. That is
    a real difference between the two clients rather than an oversight in one of
    them, so it is asserted rather than assumed away.
    """

    def setUp(self) -> None:
        """Set a dummy API key and restore the previous value afterwards"""
        previous = os.environ.get("TAVILY_API_KEY")
        os.environ["TAVILY_API_KEY"] = "tvly_test"

        def restore() -> None:
            if previous is None:
                os.environ.pop("TAVILY_API_KEY", None)
            else:
                os.environ["TAVILY_API_KEY"] = previous

        self.addCleanup(restore)

    def _search(self, payload: dict[str, Any], *, num_results: int = 5) -> Any:
        """Run ``search_tavily`` against a mocked response.

        Args:
            payload: JSON body the mocked Tavily API returns.
            num_results: Value forwarded to ``search_tavily``.

        Returns:
            The parsed results.
        """

        async def run() -> Any:
            from kindly_web_search_mcp_server.search.tavily import search_tavily

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await search_tavily("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_keeps_only_entries_carrying_three_strings(self) -> None:
        """Drop what cannot be rendered rather than emitting a half-filled result"""
        results = self._search(
            {
                "results": [
                    "not an object",
                    {"title": "No url", "content": "s"},
                    {"title": "Url is not a string", "url": 42, "content": "s"},
                    {"url": "https://no-title.example/", "content": "s"},
                    {
                        "title": "Content is not a string",
                        "url": "https://bad-snippet.example/",
                        "content": 7,
                    },
                    {"title": "Good", "url": "https://good.example/", "content": "ok"},
                ]
            }
        )

        self.assertEqual([result.title for result in results], ["Good"])

    def test_an_empty_results_list_returns_no_results(self) -> None:
        """Report a query with no hits as an empty list, not as an error"""
        self.assertEqual(self._search({"results": []}), [])

    def test_a_results_value_that_is_not_a_list_raises(self) -> None:
        """Refuse a reshaped `results`, which is Tavily's own choice here

        Driven with ``null`` for the reason recorded in the Serper and SerpBase
        equivalents: an object is iterable and would leave the container guard
        killable only by the raise, not by its own removal.
        """
        from kindly_web_search_mcp_server.search.tavily import TavilyError

        with self.assertRaises(TavilyError) as ctx:
            self._search({"results": None})

        self.assertIn("missing `results` list", str(ctx.exception))

    def test_returns_at_most_num_results(self) -> None:
        """Stop at the caller's bound even when the provider ignores `max_results`"""
        results = self._search(
            {
                "results": [
                    {"title": f"R{i}", "url": f"https://e.example/{i}", "content": "s"}
                    for i in range(5)
                ]
            },
            num_results=2,
        )

        self.assertEqual([result.title for result in results], ["R0", "R1"])


if __name__ == "__main__":
    unittest.main()

