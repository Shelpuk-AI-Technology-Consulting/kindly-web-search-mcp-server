from __future__ import annotations

import sys
from pathlib import Path
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
