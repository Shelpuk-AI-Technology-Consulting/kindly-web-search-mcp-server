from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestSearxngParsing(unittest.TestCase):
    """Cover what is specific to SearXNG: its configuration, and what each arm says.

    The transport-level failures every provider shares -- 401, 429, a non-JSON
    body, a wrong-shaped JSON body and a timeout -- live in
    ``test_search_provider_error_paths.py``, which drives all six providers from
    one table and owns the contract they share.

    ``test_search_searxng_raises_on_403``, ``..._on_429`` and
    ``..._on_invalid_json`` were **rewritten in place, not retired into it**. The
    table drives equivalent inputs, so it does not subsume them, and a rewrite
    keeps the node id -- which is the ledger's stated preference over relocating
    a claim. What stays here is the *wording* each per-status arm produces, which
    is SearXNG's own choice; the aggregate's chain is the other module's.

    Those three, and ``..._reports_an_unclassified_status_generically``, all
    assert through ``__cause__``. That is not decoration. The instance loop wraps
    every failure in one aggregate whose message quotes the error it caught, so
    the status appears in that text whichever arm produced it: measured, setting
    the 403 arm's condition to ``False`` leaves ``"403"`` in the aggregate and a
    containment check passes on a provider that no longer classifies anything.
    """

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
        """Keep only entries carrying a title, a usable URL and a snippet

        The list leads with a non-object entry on purpose. Measured: with only
        malformed *objects* in it, deleting the ``isinstance(item, dict)`` guard
        changed no result and the case passed on a provider that would raise
        ``AttributeError`` on the first string a real instance returned.

        **One entry per conjunct, not one per guard.** SearXNG's three guards
        hold seven conditions between them, and a payload that trips each *guard*
        leaves most conditions unasserted: measured, an earlier version of this
        list killed every whole-guard deletion while six single-condition
        deletions survived it. So there is an entry for a missing title, a
        non-string title, a blank title, a missing url, an unparseable url, a
        missing snippet and a blank snippet.

        **`not link.strip()` is an equivalent condition and no entry can kill
        it.** It is subsumed by `_looks_like_url` immediately after: a string
        whose `.strip()` is falsy is whitespace-only, and `urlparse` gives
        whitespace no scheme, so the URL test rejects exactly the same inputs.
        Measured over `""`, `" "`, `"   "`, `"\t\n"` and `"\xa0"`. A mutation run
        will report it as a permanent survivor; it is equivalent, not a hole, and
        no entry should be added for it.
        """

        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            payload = {
                "results": [
                    "not an object",
                    {"title": "Missing url", "content": "x"},
                    {"title": "Bad url", "url": "not-a-url", "content": "x"},
                    {"title": "Missing content", "url": "https://example.com/"},
                    {"url": "https://no-title.example/", "content": "x"},
                    {"title": 7, "url": "https://odd-title.example/", "content": "x"},
                    {"title": "   ", "url": "https://blank-title.example/", "content": "x"},
                    {"title": "Blank content", "url": "https://blank-body.example/", "content": "   "},
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

    def _failing_instance(self, response: httpx.Response) -> Exception:
        """Drive one SearXNG instance to failure and return the per-instance error.

        Every failure inside the instance loop is re-raised as one aggregate
        error, so the classifying error is reachable only through the chain. It
        is chained rather than only quoted, which is the repair this module's
        status cases were written against.

        Args:
            response: What the single configured instance answers with.

        Returns:
            The error the instance loop caught, taken from the aggregate's
            ``__cause__``.
        """

        async def run() -> Exception:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import SearxngError, search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                return response

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(SearxngError) as ctx:
                    await search_searxng("q", num_results=1, http_client=client)

            cause = ctx.exception.__cause__
            self.assertIsInstance(cause, SearxngError)
            assert isinstance(cause, Exception)
            return cause

        return anyio.run(run)

    def test_search_searxng_raises_on_403(self) -> None:
        """Tell the operator what to change on their instance, not just the status

        The 403 arm exists for one reason: a SearXNG instance answers 403 when
        the ``json`` format is not enabled, which is a configuration the operator
        owns and can fix in a minute. Asserting only that "403" appears would
        pass on the generic arm below, which says nothing actionable -- measured:
        replacing this arm's condition with ``False`` leaves the aggregate
        message still containing "403".
        """
        cause = self._failing_instance(httpx.Response(403, text="forbidden"))

        self.assertIn("403 Forbidden", str(cause))
        self.assertIn("enable the 'json' format", str(cause))

    def test_search_searxng_raises_on_429(self) -> None:
        """Name rate limiting, which is a wait rather than a misconfiguration

        Asserted as the arm's whole message. The substring "429" is not enough:
        the generic arm renders ``SearXNG returned HTTP 429.``, so a containment
        check on the number passes with this arm deleted. Measured.
        """
        cause = self._failing_instance(httpx.Response(429, text="rate limited"))

        self.assertEqual(str(cause), "SearXNG returned 429 Too Many Requests (rate limited).")

    def test_search_searxng_reports_an_unclassified_status_generically(self) -> None:
        """Still name SearXNG and the status for a code with no arm of its own

        401 is the status an operator meets when an instance sits behind
        authentication -- the case ``SEARXNG_HEADERS_JSON`` exists for -- and
        SearXNG special-cases only 403 and 429, so this is what the fallback arm
        must produce.
        """
        cause = self._failing_instance(httpx.Response(401, json={"message": "nope"}))

        self.assertEqual(str(cause), "SearXNG returned HTTP 401.")

    def test_search_searxng_raises_on_invalid_json(self) -> None:
        """Report a body that will not parse, and keep the decoder error under it

        The aggregate wrapper makes the message load-bearing here: removing the
        provider's own ``except ValueError`` arm does **not** let a raw
        ``JSONDecodeError`` escape, because the instance loop catches it and
        re-raises a ``SearxngError`` regardless. Measured. So the case asserts
        both the sentence the arm produces and the decoder error it chains to.
        """
        import json

        cause = self._failing_instance(httpx.Response(200, text="not json"))

        self.assertIn("not valid JSON", str(cause))
        self.assertIsInstance(cause.__cause__, json.JSONDecodeError)

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

    def _search(self, payload: object, *, num_results: int = 10) -> object:
        """Run ``search_searxng`` against one instance returning ``payload``.

        Args:
            payload: JSON body the mocked instance returns.
            num_results: Value forwarded to ``search_searxng``.

        Returns:
            The parsed results.
        """

        async def run() -> object:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=payload)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await search_searxng("q", num_results=num_results, http_client=client)

        return anyio.run(run)

    def test_search_searxng_raises_when_results_is_not_a_list(self) -> None:
        """Refuse a reshaped `results` instead of reporting zero hits

        Driven with ``null`` rather than with an object, and the difference is
        measured: with the guard removed, an object is iterable, the loop walks
        its **keys** -- strings, which the item guard drops -- and the function
        returns ``[]``. The case would pass on a provider that no longer checks
        the container. ``null`` is not iterable, so removal raises ``TypeError``.
        It is also the shape a JSON API sends for "nothing here".

        This raise sits *after* the instance loop, so unlike every other SearXNG
        failure it is not wrapped in the aggregate.
        """
        from kindly_web_search_mcp_server.search.searxng import SearxngError

        with self.assertRaises(SearxngError) as ctx:
            self._search({"results": None})

        self.assertIn("missing `results` list", str(ctx.exception))

    def test_search_searxng_returns_empty_when_the_instance_found_nothing(self) -> None:
        """Report a query with no hits as an empty list, not as an error

        SearXNG is the one provider that raises for a *reshaped* ``results`` and
        must still stay quiet for an *empty* one; the two are one line apart in
        the source, and only the second is a normal answer.
        """
        self.assertEqual(self._search({"results": []}), [])

    def test_search_searxng_returns_at_most_num_results(self) -> None:
        """Stop at the caller's bound, which SearXNG is never told about

        Unlike the other providers, no request parameter carries ``num_results``
        to a SearXNG instance -- the whole bound is applied locally, so this is
        the only thing enforcing it.
        """
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

    def test_search_searxng_arms_no_request_timeout_by_default(self) -> None:
        """Characterise the default: SearXNG requests carry no deadline at all

        ``_get_request_timeout_seconds`` returns ``None`` when
        ``SEARXNG_TIMEOUT_SECONDS`` is unset, and that ``None`` is passed
        explicitly to ``client.get(timeout=...)``. In httpx an explicit ``None``
        means *no timeout*; it does not fall back to the client's own
        ``timeout=30``. So the shipped default disarms the deadline rather than
        inheriting one, and the request is bounded only from outside -- by the
        server's total tool budget.

        **Characterised, not repaired.** Which deadline SearXNG should carry is a
        production decision, and this step does not change production beyond the
        exception chaining it was scoped for. Recorded in
        ``.system_design/TEST_SUITE.md`` section 14 so the choice has an owner.
        The companion case in ``test_search_provider_error_paths.py`` injects a
        timeout and so proves propagation; only this one can see arming.

        Asserted against ``request.extensions["timeout"]``, which is an httpx
        transport-extension shape rather than a product surface, and
        ``pyproject.toml`` declares ``httpx[socks]`` with **no version bound**.
        Measured on httpx 0.28.1. If a later release renames or restructures that
        extension this case reddens with no product change -- read the failure as
        a dependency note, not as a regression, and re-measure.
        """

        async def run() -> None:
            self._clear_searxng_env()
            os.environ["SEARXNG_BASE_URL"] = "https://searx.example.org"

            from kindly_web_search_mcp_server.search.searxng import search_searxng

            seen: list[object] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.extensions.get("timeout"))
                return httpx.Response(200, json={"results": []})

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport, timeout=30) as client:
                await search_searxng("q", num_results=1, http_client=client)

            self.assertEqual(
                seen, [{"connect": None, "read": None, "write": None, "pool": None}]
            )

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
