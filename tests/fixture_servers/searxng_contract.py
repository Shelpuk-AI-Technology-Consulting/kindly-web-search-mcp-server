"""A local HTTP server that answers like a SearXNG instance with JSON enabled.

This is the cross-process stubbing seam for search. The deterministic
installed-wheel product test starts the real MCP server as a **separate
process**, so ``monkeypatch`` cannot reach it, and this project deliberately
ships no provider-injection hook -- an unrestricted injection point in an
already-unauthenticated server is a larger risk than any test is worth. What is
left is configuration: ``search_searxng`` is the only one of the six providers
configured entirely by a URL, so pointing ``SEARXNG_BASE_URL`` at this module and
clearing the higher-priority provider variables makes SearXNG win selection in a
process the test cannot otherwise touch. Section 11.1 of
``.system_design/TEST_SUITE.md`` records keeping that reachable as a standing
constraint on production.

**Standard library only**, for two reasons, neither of which is purity. The
first is **circularity**: this module is the instrument that pins the SearXNG
contract, and an instrument that imported ``kindly_web_search_mcp_server`` would
be pinning that contract against the parser under test. The second is
**startability**: it is imported by a test in the job that installs the built
wheel into a fresh virtual environment, where the only third-party packages
present are the wheel's own dependencies.

The argument this deliberately does **not** rest on is that an ``httpx`` import
would break that job. It would not — ``httpx`` is a runtime dependency of the
wheel and resolves from ``site-packages`` there. A guard in
``tests/test_searxng_contract_server.py`` enforces the rule over this file's
syntax tree, so a branch that never executes is covered too.

**The contract is transcribed from SearXNG, not invented.** Read from
``searx/webapp.py``, ``searx/webutils.py`` and ``searx/result_types/_base.py`` on
``master``, 2026-09-03. What that reading changed: the response envelope has
**no** ``number_of_results`` key -- older documentation and several third-party
write-ups still show one -- and the format check runs *before* the query check,
so a disabled format is 403 even when there is no query to run.

**Where this is deliberately stricter than the real thing.** A real instance
serves both ``/`` and ``/search``; this one serves only ``/search`` and answers
404 elsewhere. Strictness is the safe direction for a fixture: it can only turn a
misconfigured caller into an investigable failure, where permissiveness would let
one pass here and fail against a real instance.

**Where this is deliberately smaller than the real thing.** There is one response
shape per instance and no configurable failure modes -- no 429, no truncated
body, no invalid JSON, no POST form endpoint, no paging. Each belongs with
whichever step first drives it; an unused mode is an unchecked one.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import socketserver
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

#: The only path this instance serves. A real instance also answers ``/``; see
#: the module docstring for why not answering it here is the safe direction.
SEARCH_PATH = "/search"

#: Formats SearXNG knows how to render. A ``format`` outside this set is not an
#: error at all: ``webapp.py`` falls back to ``html``, which is why a typo in the
#: parameter produces a web page rather than a complaint.
KNOWN_FORMATS = ("html", "json", "csv", "rss")

#: Formats *this* instance has enabled, i.e. its ``search.formats`` setting. A
#: known-but-disabled format is 403 -- the documented reason most public
#: instances fail a JSON call, and the case ``search_searxng`` carries a
#: dedicated error message for.
ENABLED_FORMATS = ("html", "json")

#: Engine name reported on every result. A single made-up name rather than a
#: real one so a snapshot of this fixture's output can never be mistaken for a
#: capture of a live instance.
FIXTURE_ENGINE = "fixture"

#: Body served for an enabled non-JSON format. Minimal on purpose: its only job
#: is to be *not JSON*, so a caller that forgot ``format=json`` fails here the
#: same way it would against a real instance.
HTML_BODY = "<!DOCTYPE html><title>fixture</title><p>results are not JSON here"

#: Seconds the serving thread is given to stop before teardown gives up on it.
#: Reached only if ``shutdown()`` itself did not return, which would be a defect
#: in this module rather than a slow machine.
JOIN_TIMEOUT_SECONDS = 10.0

#: How often ``serve_forever`` checks whether it has been asked to stop. It is
#: also the worst-case cost of every teardown, because ``shutdown()`` waits for
#: that check: measured 0.5001 s at the stdlib default of 0.5, against a module
#: that starts an instance per case. Lowered deliberately, with the trade-off
#: named -- a smaller interval is more idle wakeups and a shorter teardown.
POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class SearxngResult:
    """One main result the fixture instance will serve.

    Three fields because three are what a caller has to choose. Everything else
    a real result carries is generated, so a test's setup states its intent and
    nothing else.

    Attributes:
        title: The result's title.
        url: The result's link.
        content: The result's snippet.
    """

    title: str
    url: str
    content: str

    def as_response_item(self, position: int) -> dict[str, Any]:
        """Render this result as SearXNG renders a main result.

        The fifteen keys ``LegacyResult.__init__`` sets unconditionally, which
        is what today's engines actually emit through
        ``ResultContainer.get_ordered_results``. Not the three this project's
        parser reads: a three-key item would let a parser that accidentally
        depended on a field's absence pass here and fail against a real
        instance. Not the twenty-three of the typed ``MainResult`` either --
        that path is not the one a web result travels today, and claiming it
        would be a fidelity the fixture does not have.

        Args:
            position: This result's 1-based rank, reported in ``positions`` the
                way a single-engine result set reports it.

        Returns:
            The result item as it appears in the ``results`` array.
        """
        parsed = urlparse(self.url)
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "engine": FIXTURE_ENGINE,
            # A `set` in SearXNG, which its JSON encoder renders as a list.
            "engines": [FIXTURE_ENGINE],
            # A `ParseResult` in SearXNG, which is a `NamedTuple` and therefore
            # serializes as a six-element array, not as an object.
            "parsed_url": list(parsed),
            "template": "default.html",
            "positions": [position],
            # `weight / position` upstream (`results.py::calculate_score`), and
            # `get_ordered_results` sorts descending -- so a real instance never
            # returns results whose score *ascends*. `float(position)` would have
            # inverted that, in a module whose whole claim is transcription.
            "score": 1.0 / position,
            "category": "general",
            "publishedDate": None,
            # The four fields a web result never populates and always carries.
            # Present because `LegacyResult.__init__` sets them unconditionally,
            # so a real instance emits them empty rather than omitting them --
            # and a parser that depended on their absence would pass against a
            # fixture that omitted them and fail against the real thing.
            "img_src": "",
            "thumbnail": "",
            "priority": "",
            "iframe_src": None,
        }


def search_response(query: str, results: Sequence[SearxngResult]) -> dict[str, Any]:
    """Build the JSON body SearXNG returns for a search.

    The seven keys are exactly those ``webutils.get_json_response`` assembles,
    in its order. Kept as a module-level function so the guard that pins this
    envelope against the design document can read it without starting a server.

    Args:
        query: The query as received, echoed back the way SearXNG echoes it.
        results: The results to serve, in the order they should appear.

    Returns:
        The decoded response body.
    """
    return {
        "query": query,
        "results": [
            result.as_response_item(position)
            for position, result in enumerate(results, start=1)
        ],
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": [],
    }


class _ContractHandler(http.server.BaseHTTPRequestHandler):
    """Answer one request the way a SearXNG instance would.

    The control flow below is a transcription of ``webapp.py``'s ``search()``:
    resolve the format, reject a disabled one, then reject a missing query. The
    order matters and is not the intuitive one -- a request with no query but a
    disabled format is 403, not 400.
    """

    # `do_GET`, not `do_get`: the base class dispatches on the method name.
    def do_GET(self) -> None:
        """Serve a GET, recording it before answering.

        **The order is load-bearing and must not be tidied.** Recording after
        the response is written lets a client that has already been answered
        read the log before the handler thread appends to it, which is a race a
        caller cannot see and cannot work around. Recording first makes "the
        client got a response" imply "the request is in the log".
        """
        parsed = urlparse(self.path)
        # `parse_qs` drops blank values by default, and blank is exactly what the
        # empty-query branch has to see.
        params = {
            key: values[0]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        self.server.record(parsed.path, params)  # type: ignore[attr-defined]

        if parsed.path != SEARCH_PATH:
            self._send_bytes(404, "text/html", HTML_BODY.encode("utf-8"))
            return

        self._serve_search(params)

    def _serve_search(self, params: dict[str, str]) -> None:
        """Apply the search endpoint's own checks, in SearXNG's order.

        Args:
            params: The request's query parameters, blanks preserved.
        """
        requested = params.get("format", "html")
        # An unrecognised format is not an error upstream: it degrades to HTML.
        if requested not in KNOWN_FORMATS:
            requested = "html"

        if requested not in ENABLED_FORMATS:
            self._send_bytes(403, "text/html", HTML_BODY.encode("utf-8"))
            return

        if requested != "json":
            self._send_bytes(200, "text/html", HTML_BODY.encode("utf-8"))
            return

        # Checked after the format, which is what makes a disabled format 403
        # rather than 400 on a request that also has no query.
        if not params.get("q"):
            self._send_json(400, {"error": "No query"})
            return

        results = self.server.results  # type: ignore[attr-defined]
        self._send_json(200, search_response(params["q"], results))

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        """Send a JSON body with the status given.

        Args:
            status: HTTP status code.
            body: Payload to serialize.
        """
        self._send_bytes(
            status, "application/json", json.dumps(body).encode("utf-8")
        )

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        """Send one complete response.

        ``Content-Length`` is always set. The handler stays on the base class's
        HTTP/1.0 default deliberately: HTTP/1.1 would keep the connection alive,
        leaving a handler thread blocked on the next request line after every
        call, and ``ThreadingHTTPServer`` runs daemon threads that
        ``server_close`` does not join. Closing per response costs a connection
        and removes that whole class of leak.

        Args:
            status: HTTP status code.
            content_type: Value for the ``Content-Type`` header.
            body: Encoded response body.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # `format` shadows the builtin because the base class's signature does.
    def log_message(self, format: str, *args: Any) -> None:
        """Silence the base class's per-request line on stderr.

        The request log this fixture offers is
        :attr:`SearxngContractServer.received_requests`, which a case can assert
        on. The stderr line is the same information in a form nothing can read.
        """


class SearxngContractServer(http.server.ThreadingHTTPServer):
    """A running instance serving a fixed result set on an ephemeral port.

    Attributes:
        results: The results every search returns.
        received_requests: Path and parameters of every request answered, in
            order. This is how a caller proves it was *this* instance that
            served a search -- the observation a provider-selection test needs
            and cannot get from the provider's return value.
        serving_thread_stopped: Whether the serving thread had exited by the end
            of teardown. A recorded flag rather than an exception raised in
            ``finally``: raising there would replace the failure the case was
            already reporting with this one. ``False`` after a block has exited
            is a defect in this module.
    """

    def __init__(self, results: Sequence[SearxngResult]) -> None:
        """Bind an ephemeral loopback port and prepare to serve.

        Binding happens here, inside ``TCPServer.__init__``, which calls
        ``server_bind`` and then ``server_activate``. The socket is therefore
        *listening* before this returns, and a request that arrives before the
        serving thread is scheduled waits in the accept backlog. That is what
        makes readiness structural and lets every caller skip a handshake.

        Args:
            results: Results this instance serves. An empty sequence is a
                supported configuration and produces a well-formed zero-result
                response.
        """
        self.results = tuple(results)
        self.received_requests: list[dict[str, Any]] = []
        self.serving_thread_stopped = False
        # Handler threads append to the log, so the list is guarded even though
        # every case so far is sequential; an unguarded list is a race waiting
        # for the first caller that issues two requests at once.
        self._record_lock = threading.Lock()
        # Bound to `127.0.0.1` rather than `localhost`, which resolves to `::1`
        # first on a dual-stack host and would leave callers connecting to a port
        # nothing is listening on.
        super().__init__(("127.0.0.1", 0), _ContractHandler)

    def server_bind(self) -> None:
        """Bind without resolving a hostname.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn`` on the bound host,
        which is a name-resolution call -- 4 ms here against a cached
        ``/etc/hosts``, unbounded behind a slow resolver, and unwanted in a
        fixture that must not touch the network at all. The resolved name reaches
        only ``server_name``, which nothing outside ``CGIHTTPRequestHandler``
        reads, so skipping it costs nothing.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    @property
    def port(self) -> int:
        """Report the port the operating system assigned.

        Returns:
            The bound TCP port.
        """
        return int(self.server_address[1])

    @property
    def base_url(self) -> str:
        """Report the value to configure ``SEARXNG_BASE_URL`` with.

        Returns:
            The instance's origin, with no trailing slash -- the shape
            ``search_searxng`` appends ``/search`` to.
        """
        return f"http://127.0.0.1:{self.port}"

    def record(self, path: str, params: dict[str, str]) -> None:
        """Append one answered request to the log.

        Args:
            path: The request path, without its query string.
            params: The request's query parameters, blanks preserved.
        """
        with self._record_lock:
            self.received_requests.append({"path": path, "params": params})


@contextlib.contextmanager
def running_searxng_instance(
    results: Sequence[SearxngResult] = (),
) -> Iterator[SearxngContractServer]:
    """Run a fixture instance for the duration of a block.

    Args:
        results: Results the instance serves. Defaults to none, which is the
            zero-result configuration the product smoke test relies on to keep
            the resolver out of the picture entirely.

    Yields:
        The running instance, ready to serve.
    """
    server = SearxngContractServer(results)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": POLL_INTERVAL_SECONDS},
        name="searxng-contract-server",
        daemon=True,
    )
    # The socket is already bound and listening by the time the constructor
    # returned, so a `Thread.start()` that raises would strand it. The sibling
    # fixture step's review found this exact shape in a process spawn.
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise

    try:
        yield server
    # In `finally`, because the leak only ever matters on the run that was
    # already failing: on a passing run nobody notices a socket that stayed open.
    #
    # None of these three can hang on a wedged handler, which is the failure a
    # reviewer will reach for. `serve_forever` runs the accept loop only, so a
    # stuck handler does not delay `shutdown()`; and although
    # `ThreadingMixIn.server_close` ends in an untimed `self._threads.join()`,
    # `_Threads.append` returns early for daemon threads and
    # `ThreadingHTTPServer` sets `daemon_threads = True`, so that list is always
    # empty. Measured against a handler sleeping 30 s: `shutdown()` 0.5001 s at
    # the default poll interval, `server_close()` 0.0001 s.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        server.serving_thread_stopped = not thread.is_alive()
