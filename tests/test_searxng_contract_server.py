"""Calibration for the local SearXNG-contract HTTP server fixture.

``tests/fixture_servers/searxng_contract.py`` is a test-only stand-in for a self-hosted
SearXNG instance with the JSON output format enabled. It exists because the
deterministic installed-wheel product test drives the MCP server in a **separate
process**, where ``monkeypatch`` cannot reach and where this project deliberately
offers no provider-injection hook in shipped code. Configuration is the only
stubbing mechanism left, and ``search_searxng`` -- alone among the six providers
-- is configured entirely by a URL. Section 11.1 of
``.system_design/TEST_SUITE.md`` records keeping that true as a standing
constraint on production.

An instrument needs its own calibration. Most of these cases drive the fixture
directly over a real socket with ``urllib.request``, deliberately not through
``httpx``: the fixture's job is to be correct for *any* client, and a case that
could only ever be exercised by the one client this project ships would not
notice a fixture that had quietly become ``httpx``-shaped.

The rest drive **production** against it -- ``search_searxng`` for parsing and
``search_web`` for provider selection -- which is what the step's verify clause
asks for and what a fixture-only case cannot claim.

**Readiness here is structural, not polled.** ``socketserver.TCPServer.__init__``
binds *and* listens, so the port is accepting connections before the constructor
returns and a request arriving before the serving thread is scheduled waits in
the accept backlog. That is why no case here sleeps, retries or asserts a startup
budget: there is no window to wait out. Measured on CPython 3.13.15 -- a
connection opened before ``serve_forever()`` was ever called was answered 200.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.search import (
    provider_env_vars,
    search_searxng,
    search_web,
)
from kindly_web_search_mcp_server.utils.diagnostics import Diagnostics

# `_section_body` is imported rather than copied: the fence-aware section bound
# is one helper with four users already, and a fifth copy would be a fifth thing
# to fix.
from tests.fixture_servers.searxng_contract import (
    SearxngResult,
    running_searxng_instance,
)
from tests.test_pytest_configuration import _section_body

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The fixture package, addressed by path for the import guard, which reads
#: syntax trees rather than the imported modules' namespaces. The whole package
#: and not just one module in it: the job that consumes this imports
#: ``tests.fixture_servers.searxng_contract``, which runs ``__init__.py`` too.
FIXTURE_SERVER_PACKAGE = REPO_ROOT / "tests" / "fixture_servers"

#: Ceiling on every request these cases make. Section 5.4 requires a per-test
#: bound; expressed per call rather than per case so a wedged fixture fails at
#: the request that wedged, naming it, instead of at a whole-case deadline that
#: names nothing.
#:
#: **It has to be applied twice, by two different mechanisms.** The cases that
#: drive the fixture directly pass it to ``urllib``. The three that drive
#: production cannot: ``search_searxng`` reads ``SEARXNG_TIMEOUT_SECONDS`` and
#: passes the result to ``client.get(timeout=...)``, and with that variable
#: cleared -- which the hermetic sweep below requires -- the value is an
#: explicit ``None``, which **overrides** ``AsyncClient(timeout=30)`` rather than
#: deferring to it. Measured on httpx 0.28.1 against a handler sleeping 20 s: the
#: client default raised ``ReadTimeout`` at 3.09 s, the explicit ``None`` was
#: still running when the probe gave up. So those three wrap the await in
#: ``asyncio.timeout`` instead.
HTTP_TIMEOUT_SECONDS = 10.0

#: The result set most cases serve. Two entries rather than one, because a
#: single-result fixture cannot tell "returns the results" from "returns the
#: first result", and the ordering claim is what a paging bug would break.
SAMPLE_RESULTS = (
    SearxngResult(
        title="Kindly Web Search",
        url="https://example.com/kindly",
        content="The first snippet.",
    ),
    SearxngResult(
        title="Another Page",
        url="https://example.org/another",
        content="The second snippet.",
    ),
)


def _get(url: str) -> tuple[int, bytes]:
    """Issue a GET and return its status and body, errors included.

    ``urllib.request`` raises on a 4xx rather than returning it, and every
    rejection case here needs the status *and* the body. Both arms are collapsed
    into one return shape so a case reads the same whichever it took.

    Args:
        url: Absolute URL to request.

    Returns:
        The response status code and the raw response body.
    """
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    # `HTTPError` is both an exception and a response object; reading it here is
    # what lets the 400 case assert the error body a real instance returns.
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


@pytest.mark.subsystem
def test_two_instances_bind_different_ephemeral_ports() -> None:
    """Prove the port is chosen by the operating system, not by the fixture

    A fixed port is the classic way a server fixture becomes unrunnable in
    parallel and flaky under a developer who happens to have something else
    bound. Two live instances at once is the only observation that distinguishes
    an ephemeral port from a constant that has not collided yet.
    """
    with (
        running_searxng_instance(SAMPLE_RESULTS) as first,
        running_searxng_instance(SAMPLE_RESULTS) as second,
    ):
        assert first.port != second.port

        for instance in (first, second):
            status, _ = _get(f"{instance.base_url}/search?q=ping&format=json")
            assert status == 200, f"{instance.base_url} did not answer"


@pytest.mark.subsystem
def test_it_answers_the_first_request_with_no_wait() -> None:
    """Pin readiness as structural, so nothing here grows a sleep

    The request below is the first statement inside the block. It is not a
    timing assertion and must never become one: it fails only if the fixture
    stops listening before it yields, which is a real defect, and it cannot fail
    because a machine is loaded.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        status, _ = _get(f"{instance.base_url}/search?q=ping&format=json")

    assert status == 200


@pytest.mark.subsystem
def test_the_port_is_released_when_the_block_exits() -> None:
    """Prove the instance is gone, not merely unreferenced

    A fixture that leaks its listening socket exhausts nothing quickly enough to
    be noticed, and the next test to bind the same port inherits a stranger's
    handler. The observation is a refused connection: the fixture no longer
    accepts.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        port = instance.port

    # Two positive observations before the negative one, because "the connection
    # was refused" is also what a port reassigned to a stranger would *not*
    # produce, and the negative alone cannot tell those apart.
    assert instance.socket.fileno() == -1, "the listening socket is still open"
    assert instance.serving_thread_stopped, "the serving thread outlived the block"

    with pytest.raises(OSError):
        # `create_connection`, not an HTTP request: the claim is about the
        # listening socket, and a TCP connect answers it without a protocol in
        # the way.
        socket.create_connection(("127.0.0.1", port), timeout=HTTP_TIMEOUT_SECONDS).close()


@pytest.mark.subsystem
def test_a_failure_inside_the_block_still_releases_the_port() -> None:
    """Prove the teardown is in ``finally`` and not on the happy path

    This is the case that matters in practice: cleanup runs when a test passes
    whether or not anyone wrote it correctly, and the leak only ever appears on
    the run that was already failing.
    """
    sentinel = RuntimeError("the case under this fixture failed")

    with (
        pytest.raises(RuntimeError) as raised,
        running_searxng_instance(SAMPLE_RESULTS) as instance,
    ):
        port = instance.port
        raise sentinel

    assert raised.value is sentinel
    assert instance.socket.fileno() == -1, "the listening socket is still open"
    assert instance.serving_thread_stopped, "the serving thread outlived the block"

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=HTTP_TIMEOUT_SECONDS).close()


#: The design document's own section for this fixture. Bounded rather than
#: searched document-wide: an unbounded scrape starts comparing against some
#: later section's table or block the day one is added.
DESIGN_SECTION_HEADING = "### 5.2b The SearXNG-contract fixture server — built in E3-2"

#: How many rows the design document's contract table must carry. Pinned so that
#: deleting a row fails here rather than quietly shrinking the sweep to green --
#: the house rule is to pin the set as well as each member.
DOCUMENTED_REQUEST_COUNT = 5


def _design_section() -> str:
    """Return the body of the design document's section for this fixture.

    Returns:
        Every line of section 5.2b, fenced blocks included.
    """
    text = (REPO_ROOT / ".system_design" / "TEST_SUITE.md").read_text(encoding="utf-8")
    # Checked before delegating: `_section_body` locates the heading with
    # `list.index`, whose `ValueError` names the string but not what it was for.
    assert DESIGN_SECTION_HEADING in text.splitlines(), (
        f"{DESIGN_SECTION_HEADING!r} is not a heading in TEST_SUITE.md; the "
        "guards below read the fixture's contract out of that section"
    )
    return _section_body(text, DESIGN_SECTION_HEADING)


def _documented_specimen() -> dict[str, Any]:
    """Parse the specimen response the design document records.

    Requires **exactly one** fenced JSON block in the section. "The only block
    in the section" is a claim that quietly becomes false the day a second one
    is added, so it is asserted rather than assumed.

    Returns:
        The decoded specimen body.
    """
    blocks = re.findall(r"^```json\n(.*?)^```", _design_section(), re.DOTALL | re.MULTILINE)
    assert len(blocks) == 1, f"expected one JSON block in {DESIGN_SECTION_HEADING}, found {len(blocks)}"
    return json.loads(blocks[0])


def _documented_requests() -> list[tuple[str, int, str]]:
    """Parse the request/answer table the design document records.

    Returns:
        One ``(request target, status, content type)`` triple per table row.
    """
    rows = re.findall(
        r"^\| `GET (\S+)` \| `(\d{3})` \| `([^`]+)` \|",
        _design_section(),
        re.MULTILINE,
    )
    # The COUNT, not just non-emptiness. Re-measured against this module as it
    # stands: with only `assert rows`, deleting the `/elsewhere` row from the
    # design document leaves it reporting 17 passed -- the sweep silently shrinks
    # instead of failing, which is precisely the cheap escape the reviewer rule
    # file names for this guard. With the count pinned the same deletion fails at
    # *collection* ("parsed 4 request rows"), which is louder still.
    assert len(rows) == DOCUMENTED_REQUEST_COUNT, (
        f"parsed {len(rows)} request rows out of {DESIGN_SECTION_HEADING}, "
        f"expected {DOCUMENTED_REQUEST_COUNT}"
    )
    return [(target, int(status), content_type) for target, status, content_type in rows]


@pytest.mark.subsystem
@pytest.mark.parametrize(("target", "status", "content_type"), _documented_requests())
def test_every_documented_request_gets_its_documented_status(
    target: str, status: int, content_type: str
) -> None:
    """Drive the design document's contract table against a live instance

    The table is the specification of what this instance answers, and driving it
    is what stops it becoming decoration: a row nobody implemented fails here,
    and so does a documented answer that later changed.

    **What it does not catch, measured:** a route with no row at all. Adding an
    undocumented ``/healthz`` returning 200 leaves this module reporting 18
    passed -- its full count, so nothing anywhere notices.
    The guard compares the fixture against the table row by row; it cannot see a
    behaviour the table never mentions. Said plainly here because the earlier
    wording claimed otherwise, and a reader who believed it would skip
    documenting the next route they added.

    The rejection rows are the ones that matter. A fixture that answered JSON to
    every request would be *more permissive than a real instance*, and a caller
    that had forgotten ``format=json`` would pass here and fail against the real
    thing -- the failure mode a stand-in exists to prevent.

    Args:
        target: Request target, path and query string, from the table.
        status: The status code the table records.
        content_type: The content type the table records.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        request = urllib.request.Request(f"{instance.base_url}{target}")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                observed, headers = response.status, response.headers
        except urllib.error.HTTPError as error:
            with error:
                observed, headers = error.code, error.headers

    assert observed == status
    assert headers.get_content_type() == content_type


@pytest.mark.subsystem
def test_the_served_response_matches_the_documented_specimen() -> None:
    """Pin the whole response body against the design document, value by value

    Equality against a recorded specimen rather than a check that some expected
    field names are present. A name-by-name comparison passes on a body that
    merely looks plausible: it cannot see a field with the wrong JSON *type*
    (``engines`` is a ``set`` upstream and must render as an array;
    ``parsed_url`` is a ``NamedTuple`` and must render as a six-element array,
    not an object), an extra field nobody expected, or the ``number_of_results``
    key that older SearXNG documentation still shows and current SearXNG does
    not emit.

    The instance is configured **from the specimen**, so the document is the one
    place the expected title, URL and snippet are written. A second copy in this
    file would be free to drift from it.
    """
    specimen = _documented_specimen()
    assert len(specimen["results"]) == 1, "the specimen must configure exactly one result"
    documented = specimen["results"][0]

    configured = SearxngResult(
        title=documented["title"], url=documented["url"], content=documented["content"]
    )
    with running_searxng_instance([configured]) as instance:
        status, body = _get(
            f"{instance.base_url}/search"
            f"?q={urllib.parse.quote(specimen['query'])}&format=json"
        )

    assert status == 200
    assert json.loads(body) == specimen


@pytest.mark.subsystem
def test_it_serves_the_result_set_it_was_configured_with() -> None:
    """Prove the result set is the caller's, and that its order survives

    Two results rather than one: a single-result fixture cannot tell "serves the
    results" from "serves the first result", and order is what a paging or
    sorting mistake would disturb.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        status, body = _get(f"{instance.base_url}/search?q=kindly&format=json")

    assert status == 200
    served = json.loads(body)["results"]
    assert [item["title"] for item in served] == [result.title for result in SAMPLE_RESULTS]
    assert [item["url"] for item in served] == [result.url for result in SAMPLE_RESULTS]
    assert [item["content"] for item in served] == [
        result.content for result in SAMPLE_RESULTS
    ]


@pytest.mark.subsystem
def test_a_zero_result_configuration_serves_an_empty_result_list() -> None:
    """Prove the zero-result mode is a well-formed response, not an error

    This is the configuration §6.1's product smoke test runs in, and the reason
    it can run at all: on an empty result set ``web_search`` short-circuits
    before the enrichment fan-out, so no resolver runs and no browser is
    launched in a job that has none. A fixture that signalled "no results" with
    a 404 or a missing key would send that test down the error path instead.
    """
    with running_searxng_instance() as instance:
        status, body = _get(f"{instance.base_url}/search?q=kindly&format=json")

    assert status == 200
    assert json.loads(body)["results"] == []


@pytest.mark.subsystem
def test_it_records_the_searches_it_answered() -> None:
    """Prove the instance can say what reached it

    A provider's return value cannot identify which provider produced it, and an
    empty result list looks the same whether this instance served it or whether
    nothing was called at all. The recorded request is the observation that
    tells those apart, and §6.1's selection claim rests on it.

    **One mutant survives here, and it is triaged rather than left silent.** The
    handler records *before* it writes any response byte, which is what makes
    "the client got a response" imply "the request is in the log". Moving the
    call after the write leaves this module reporting 18 passed -- its full count,
    re-measured 5 runs out of 5. The window between a client's last read and a handler thread's
    next statement is too narrow for a synchronous case to land in, and closing
    it deterministically would need a fixture knob whose only user is this test.
    So the ordering is held by the comment on ``do_GET`` and by §5.2b, not by an
    assertion, and the reason is written here rather than discovered again. The
    lock around the append is undriven for the same reason: no case here issues
    two concurrent requests.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        _get(f"{instance.base_url}/search?q=kindly&format=json")
        recorded = list(instance.received_requests)

    assert len(recorded) == 1
    assert recorded[0]["path"] == "/search"
    assert recorded[0]["params"]["q"] == "kindly"
    assert recorded[0]["params"]["format"] == "json"


@pytest.mark.subsystem
def test_a_search_with_no_query_returns_searxngs_own_error_body() -> None:
    """Pin the rejection body, which the status alone does not reach

    The design document's contract table records this row's body as
    ``{"error": "No query"}``, and until this case existed that sentence was
    prose nothing checked -- the table-driven guard asserts the status and the
    content type, so a `400` carrying any other JSON satisfied it.

    The body is upstream's, not this project's: ``index_error`` produces it, and
    it is JSON only because the request asked for JSON. A caller that parsed the
    error would break on a fixture that invented its own shape.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        status, body = _get(f"{instance.base_url}/search?q=&format=json")

    assert status == 400
    assert json.loads(body) == {"error": "No query"}


def test_the_fixture_server_imports_only_the_standard_library() -> None:
    """Keep the fixture importable in the job that installs the wheel

    §6.1's `package` job installs the built wheel into a fresh virtual
    environment and asserts the server module resolves under ``site-packages``
    rather than under the checkout. This module is imported by a test in that
    job. An ``httpx`` import -- or anything under ``src/`` -- would drag a
    checkout-resolved import into the one job whose purpose is proving the wheel
    stands alone.

    Asserted over the module's syntax tree rather than by importing it, so a
    branch that never executes is covered too.
    """
    modules = sorted(FIXTURE_SERVER_PACKAGE.glob("*.py"))
    assert modules, f"no modules found under {FIXTURE_SERVER_PACKAGE}"

    roots: set[str] = set()
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # A relative import inside `tests.fixture_servers` would resolve,
                # and would reach whatever that package grows later; the point is
                # to keep the dependencies visible in one list.
                assert node.level == 0, f"{module.name} must not import relatively"
                if node.module:
                    roots.add(node.module.split(".")[0])

    assert roots, "the import sweep found nothing, so it is asserting nothing"
    outside = sorted(roots - set(sys.stdlib_module_names))
    assert not outside, f"the fixture package imports non-stdlib modules: {outside}"


#: Result ceiling every provider call here passes. Comfortably above
#: ``len(SAMPLE_RESULTS)`` on purpose: ``search_searxng`` truncates at this
#: number, and a case that left truncation live could not say whether a missing
#: result was dropped by the parser or never served.
NUM_RESULTS = 10


@contextlib.contextmanager
def _only_searxng_configured(base_url: str) -> Iterator[None]:
    """Run a block with SearXNG as the only thing production can read.

    ``clear=True``, not a delete-list. A list clears the variables somebody
    thought of, and this code reads more of them than the six that select a
    provider: eight ``SEARXNG_*`` tuning variables, of which
    ``SEARXNG_TIMEOUT_SECONDS`` would make these cases flaky and
    ``SEARXNG_HEADERS_JSON`` would make them raise before a socket is opened.
    Worse, production builds its ``httpx.AsyncClient`` with ``trust_env`` left at
    its default, so an ambient ``ALL_PROXY`` or ``HTTP_PROXY`` sends a request
    aimed at loopback through a proxy instead. This repository already carries
    two separate defences against that trap elsewhere, which is the evidence that
    it bites.

    Clearing everything also covers the variable nobody has added yet -- the
    failure that broke the ledger guard the day a sixth provider landed.

    Args:
        base_url: Origin of the running fixture instance.

    Yields:
        Nothing; the environment is restored on exit.
    """
    with patch.dict(os.environ, {"SEARXNG_BASE_URL": base_url}, clear=True):
        yield


@pytest.mark.subsystem
async def test_the_provider_parses_the_fixtures_results_over_a_real_socket() -> None:
    """Drive ``search_searxng`` end to end against the fixture

    No ``http_client`` is supplied, so the provider builds its own and the
    transport is real from the call to the socket -- which is the half a
    ``MockTransport`` unit test structurally cannot reach, and the half that
    breaks when a URL is assembled wrongly.

    The recorded request is asserted alongside the parsed results. Without it a
    provider that somehow answered from nowhere would look identical to one that
    made the call.
    """
    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        with _only_searxng_configured(instance.base_url):
            async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                results = await search_searxng("kindly", num_results=NUM_RESULTS)
        recorded = list(instance.received_requests)

    assert [result.title for result in results] == [r.title for r in SAMPLE_RESULTS]
    assert [result.link for result in results] == [r.url for r in SAMPLE_RESULTS]
    assert [result.snippet for result in results] == [r.content for r in SAMPLE_RESULTS]

    assert len(recorded) == 1, recorded
    assert recorded[0]["params"]["q"] == "kindly"


@pytest.mark.subsystem
async def test_the_router_selects_searxng_when_only_its_url_is_configured() -> None:
    """Prove the cross-process seam §6.1 depends on actually selects SearXNG

    Selection is proven **two ways, because neither alone is sufficient**. The
    emitted diagnostic names the provider the router *chose*, which a router
    that then called nothing would still emit. The fixture's own record proves a
    request arrived, which on its own could not say which provider sent it.
    Together they are the claim.

    The higher-priority variables are asserted absent rather than assumed
    absent: a developer with a real ``SERPER_API_KEY`` exported is the ordinary
    case, and a case that only passed on a clean machine would be no control at
    all.
    """
    diagnostics = Diagnostics(request_id="fixture", enabled=True, stream=io.StringIO())

    with running_searxng_instance(SAMPLE_RESULTS) as instance:
        with _only_searxng_configured(instance.base_url):
            leftovers = [
                name
                for name in provider_env_vars()
                if name != "SEARXNG_BASE_URL" and name in os.environ
            ]
            assert not leftovers, f"a higher-priority provider is still configured: {leftovers}"

            async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                results = await search_web(
                    "kindly", num_results=NUM_RESULTS, diagnostics=diagnostics
                )
        recorded = list(instance.received_requests)

    selections = [
        entry for entry in diagnostics.entries if entry["stage"] == "search.provider_select"
    ]
    assert len(selections) == 1, diagnostics.entries
    assert selections[0]["data"]["provider"] == "searxng"

    assert len(recorded) == 1, recorded
    assert [result.title for result in results] == [r.title for r in SAMPLE_RESULTS]


@pytest.mark.subsystem
async def test_a_zero_result_instance_reaches_the_router_caller_as_an_empty_list() -> None:
    """Prove the empty list §6.1 relies on comes from the instance

    The product smoke test runs in exactly this configuration, and its
    determinism rests on it: an empty result set makes ``web_search``
    short-circuit before the enrichment fan-out, so no resolver runs and no
    browser is launched in a job that has none.

    The recorded request carries the whole weight of the case. ``[]`` is also
    what a router that called nothing at all would return, so without it this
    would pass against a completely disconnected fixture.
    """
    with running_searxng_instance() as instance:
        with _only_searxng_configured(instance.base_url):
            async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                results = await search_web("kindly", num_results=NUM_RESULTS)
        recorded = list(instance.received_requests)

    assert results == []
    assert len(recorded) == 1, recorded
