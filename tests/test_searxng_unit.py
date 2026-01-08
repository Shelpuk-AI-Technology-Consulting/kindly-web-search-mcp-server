from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestSearxngParsing(unittest.TestCase):
    @staticmethod
    def _clear_searxng_env() -> None:
        for key in (
            "SEARXNG_BASE_URL",
            "SEARXNG_LANGUAGE",
            "SEARXNG_CATEGORIES",
            "SEARXNG_ENGINES",
            "SEARXNG_TIME_RANGE",
            "SEARXNG_SAFESEARCH",
            "SEARXNG_HEADERS_JSON",
            "SEARXNG_TIMEOUT_SECONDS",
            "SEARXNG_USER_AGENT",
        ):
            os.environ.pop(key, None)

    def test_search_searxng_parses_results(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org/"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            payload = {
                "query": "searxng",
                "results": [
                    {
                        "title": "Example",
                        "url": "https://example.com/",
                        "content": "Snippet text",
                    }
                ],
            }

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                self.assertEqual(str(request.url.copy_with(query=None)), "https://searx.example.org/search")
                params = dict(request.url.params)
                self.assertEqual(params.get("q"), "searxng")
                self.assertEqual(params.get("format"), "json")
                self.assertIn("user-agent", {k.lower() for k in request.headers.keys()})
                return httpx.Response(200, json=payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_searxng("searxng", num_results=1, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Example")
            self.assertEqual(results[0].link, "https://example.com/")
            self.assertTrue(results[0].snippet)

        anyio.run(run)

    def test_search_searxng_passes_optional_params_and_headers(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"
            os.environ["SEARXNG_LANGUAGE"] = "en-US"
            os.environ["SEARXNG_CATEGORIES"] = "general"
            os.environ["SEARXNG_ENGINES"] = "google,bing"
            os.environ["SEARXNG_TIME_RANGE"] = "day"
            os.environ["SEARXNG_SAFESEARCH"] = "1"
            os.environ["SEARXNG_HEADERS_JSON"] = '{"X-Test": "1"}'
            os.environ["SEARXNG_USER_AGENT"] = "MyUA/1.0"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                params = dict(request.url.params)
                self.assertEqual(params.get("language"), "en-US")
                self.assertEqual(params.get("categories"), "general")
                self.assertEqual(params.get("engines"), "google,bing")
                self.assertEqual(params.get("time_range"), "day")
                self.assertEqual(params.get("safesearch"), "1")
                self.assertEqual(request.headers.get("X-Test"), "1")
                self.assertEqual(request.headers.get("User-Agent"), "MyUA/1.0")
                return httpx.Response(200, json={"results": []})

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_searxng("q", num_results=1, http_client=client)
            self.assertEqual(results, [])

        anyio.run(run)

    def test_search_searxng_skips_malformed_items(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            payload = {
                "results": [
                    {"title": "Missing url", "content": "x"},
                    {"title": "Bad url", "url": "not-a-url", "content": "x"},
                    {"title": "Missing content", "url": "https://example.com/"},
                    {"title": "Good", "url": "https://good.example/", "content": "ok"},
                ]
            }

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                results = await search_searxng("q", num_results=10, http_client=client)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Good")

        anyio.run(run)

    def test_search_searxng_raises_on_403(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import SearxngError, search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(403, text="forbidden")

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(SearxngError) as ctx:
                    await search_searxng("q", num_results=1, http_client=client)
            self.assertIn("403", str(ctx.exception))

        anyio.run(run)

    def test_search_searxng_raises_on_429(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import SearxngError, search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(429, text="rate limited")

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(SearxngError) as ctx:
                    await search_searxng("q", num_results=1, http_client=client)
            self.assertIn("429", str(ctx.exception))

        anyio.run(run)

    def test_search_searxng_raises_on_invalid_json(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import SearxngError, search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, text="not json")

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(SearxngError) as ctx:
                    await search_searxng("q", num_results=1, http_client=client)
            self.assertIn("not valid JSON", str(ctx.exception))

        anyio.run(run)

    def test_search_searxng_raises_on_invalid_headers_json(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"
            os.environ["SEARXNG_HEADERS_JSON"] = "not-json"

            from kindly_web_search_mcp_server.search.searxng import SearxngConfigError, search_searxng

            def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
                return httpx.Response(200, json={"results": []})

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(SearxngConfigError):
                    await search_searxng("q", num_results=1, http_client=client)

        anyio.run(run)

    def test_search_searxng_rejects_invalid_base_url(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "not a url"

            from kindly_web_search_mcp_server.search.searxng import SearxngConfigError, search_searxng

            with self.assertRaises(SearxngConfigError):
                await search_searxng("q", num_results=1)

        anyio.run(run)

    def test_search_searxng_user_agent_from_headers_json_wins(self) -> None:
        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"
            os.environ["SEARXNG_USER_AGENT"] = "EnvUA/1.0"
            os.environ["SEARXNG_HEADERS_JSON"] = '{"User-Agent":"JsonUA/2.0"}'

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            captured_user_agent = None

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal captured_user_agent
                captured_user_agent = request.headers.get("User-Agent")
                return httpx.Response(200, json={"results": []})

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                await search_searxng("q", num_results=1, http_client=client)

            self.assertEqual(captured_user_agent, "JsonUA/2.0")

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
