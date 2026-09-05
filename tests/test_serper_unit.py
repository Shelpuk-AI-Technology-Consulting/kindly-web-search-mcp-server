from __future__ import annotations

import sys
from pathlib import Path
import os
import unittest

from typing import Any

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestSerperParsing(unittest.TestCase):
    def test_search_serper_parses_organic_results(self) -> None:
        async def run() -> None:
            os.environ["SERPER_API_KEY"] = "test_key"

            from kindly_web_search_mcp_server.search.serper import search_serper

            serper_payload = {
                "searchParameters": {"q": "apple inc", "type": "search", "engine": "google"},
                "organic": [
                    {
                        "title": "Apple",
                        "link": "https://www.apple.com/",
                        "snippet": "Discover the innovative world of Apple…",
                        "position": 1,
                    },
                    {
                        "title": "Apple Inc. - Wikipedia",
                        "link": "https://en.wikipedia.org/wiki/Apple_Inc.",
                        "snippet": "Apple Inc. is an American multinational…",
                        "position": 2,
                    },
                ],
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), "https://google.serper.dev/search")
                self.assertEqual(request.headers.get("x-api-key"), "test_key")
                return httpx.Response(200, json=serper_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_serper("apple inc", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Apple")
            self.assertEqual(results[0].link, "https://www.apple.com/")
            self.assertTrue(results[0].snippet)

        anyio.run(run)


class TestSerperItemParsing(unittest.TestCase):
    """Cover the item loop: which entries survive it, and where it stops.

    Serper is the default provider -- the first entry in the selection order, and
    the one a deployment with no other credential uses -- yet until now the only
    thing asserted about it was that a well-formed response parses. Everything
    below is about responses that are *not* well-formed, which is the shape a
    schema change arrives in.
    """

    def setUp(self) -> None:
        """Set a dummy API key and restore the previous value afterwards"""
        previous = os.environ.get("SERPER_API_KEY")
        os.environ["SERPER_API_KEY"] = "test_key"

        def restore() -> None:
            if previous is None:
                os.environ.pop("SERPER_API_KEY", None)
            else:
                os.environ["SERPER_API_KEY"] = previous

        self.addCleanup(restore)

    def _search(self, payload: dict[str, Any], *, num_results: int = 5) -> Any:
        """Run ``search_serper`` against a mocked response.

        Args:
            payload: JSON body the mocked Serper API returns.
            num_results: Value forwarded to ``search_serper``.

        Returns:
            The parsed results.
        """

        async def run() -> Any:
            from kindly_web_search_mcp_server.search.serper import search_serper

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await search_serper("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_keeps_only_entries_carrying_three_strings(self) -> None:
        """Drop what cannot be rendered rather than emitting a half-filled result"""
        results = self._search(
            {
                "organic": [
                    "not an object",
                    {"title": "No link", "snippet": "s"},
                    {"title": "Link is not a string", "link": 42, "snippet": "s"},
                    {"link": "https://no-title.example/", "snippet": "s"},
                    {
                        "title": "Snippet is not a string",
                        "link": "https://bad-snippet.example/",
                        "snippet": 7,
                    },
                    {"title": "Good", "link": "https://good.example/", "snippet": "ok"},
                ]
            }
        )

        self.assertEqual([result.title for result in results], ["Good"])

    def test_an_empty_organic_list_returns_no_results(self) -> None:
        """Report a query with no hits as an empty list, not as an error"""
        self.assertEqual(self._search({"organic": []}), [])

    def test_an_organic_value_that_is_not_a_list_returns_no_results(self) -> None:
        """Survive a response whose `organic` is not a list

        Returning ``[]`` -- rather than raising, as Tavily does for the same
        shape -- is this provider's own behaviour, so the case pins the
        difference rather than assuming the providers agree.

        Driven with ``null`` rather than with an object, and the difference is
        measured, not stylistic: an object is iterable, so with the guard removed
        the loop walks its **keys** -- strings, which the item guard drops -- and
        the function still returns ``[]``. The case would pass on a provider that
        no longer checks the container at all. ``null`` is not iterable, so
        removing the guard raises ``TypeError`` and the case fails. It is also
        the shape a JSON API actually sends for "nothing here".
        """
        self.assertEqual(self._search({"organic": None}), [])

    def test_returns_at_most_num_results(self) -> None:
        """Stop at the caller's bound even when the provider ignores `num`

        ``num`` is sent to Serper as a request parameter, but nothing obliges the
        API to honour it, and this server's own tool contract is what the caller
        sees. So the bound is applied again on the way out.
        """
        results = self._search(
            {
                "organic": [
                    {"title": f"R{i}", "link": f"https://e.example/{i}", "snippet": "s"}
                    for i in range(5)
                ]
            },
            num_results=2,
        )

        self.assertEqual([result.title for result in results], ["R0", "R1"])


if __name__ == "__main__":
    unittest.main()
