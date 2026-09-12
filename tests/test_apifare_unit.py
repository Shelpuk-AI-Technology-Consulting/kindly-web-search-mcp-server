from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestApifareParsing(unittest.TestCase):
    def setUp(self) -> None:
        """Set a dummy token and restore the previous value afterwards"""
        previous = os.environ.get("APIFARE_TOKEN")
        os.environ["APIFARE_TOKEN"] = "apifare_test"

        def restore() -> None:
            if previous is None:
                os.environ.pop("APIFARE_TOKEN", None)
            else:
                os.environ["APIFARE_TOKEN"] = previous

        self.addCleanup(restore)

    def test_search_apifare_parses_results(self) -> None:
        async def run() -> None:
            from kindly_web_search_mcp_server.search.apifare import search_apifare

            apifare_payload = {
                "result": {
                    "query": "leo messi",
                    "results": [
                        {
                            "title": "Lionel Messi Facts | Britannica",
                            "url": "https://www.britannica.com/facts/Lionel-Messi",
                            "description": "Lionel Messi, an Argentine footballer...",
                            "position": 1,
                        }
                    ],
                },
                "cost_usd": 0.002,
                "credits_charged": 0.3,
                "balance": 99.7,
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), "https://apifare.com/v1/call/dataforseo")
                self.assertEqual(request.headers.get("authorization"), "Bearer apifare_test")
                return httpx.Response(200, json=apifare_payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_apifare("leo messi", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Lionel Messi Facts | Britannica")
            self.assertEqual(results[0].link, "https://www.britannica.com/facts/Lionel-Messi")
            self.assertTrue(results[0].snippet)

        anyio.run(run)

    def test_402_surfaces_the_topup_url_without_the_token(self) -> None:
        """An empty balance is an actionable message, not a bare status.

        apifare's 402 body carries a ``topup_url`` built from the account's
        public reference. The raised message must include that URL (the agent
        relays it to the operator) and must not include the bearer token.
        """

        async def run() -> None:
            from kindly_web_search_mcp_server.search.apifare import (
                ApifarePaymentRequiredError,
                search_apifare,
            )

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    402,
                    json={
                        "error": "PAYMENT_REQUIRED",
                        "message": "Balance too low. Top up and retry.",
                        "topup_url": "https://apifare.com/topup?ref=pr_example",
                        "balance": 0,
                    },
                )

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(ApifarePaymentRequiredError) as ctx:
                    await search_apifare("anything", num_results=1, http_client=client)

            message = str(ctx.exception)
            self.assertIn("https://apifare.com/topup?ref=pr_example", message)
            self.assertNotIn("apifare_test", message)

        anyio.run(run)

    def test_missing_token_raises_config_error(self) -> None:
        async def run() -> None:
            from kindly_web_search_mcp_server.search.apifare import (
                ApifareConfigError,
                search_apifare,
            )

            os.environ.pop("APIFARE_TOKEN", None)
            with self.assertRaises(ApifareConfigError):
                await search_apifare("anything", num_results=1)

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
