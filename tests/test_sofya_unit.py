from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
