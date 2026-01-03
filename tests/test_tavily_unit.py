from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

