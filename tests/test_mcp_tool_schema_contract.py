"""Pin the tool contract this server advertises to MCP clients.

FastMCP derives each tool's JSON Schema from its Python signature and its
description from its docstring, then serves both over
:meth:`~mcp.server.fastmcp.FastMCP.list_tools`. That payload is this server's
public API: renaming a parameter breaks every client, and until this file existed
nothing in the suite noticed.

The comparison is normalized before it is made. Generated ``title`` keywords are
dropped and description *wording* is replaced by a sentinel, because the allowed
SDK range (``mcp>=1.25,<2``) may reorder keys or rewrite generated text in a minor
release. Nothing else is dropped: a key a future SDK adds fails the golden, which
is what a golden over a public API is for. Measured on 2026-09-06, the payload is
in fact byte-identical on ``1.25.0`` and ``1.29.1``, titles included, so the
normalization is a forward guard rather than a compatibility shim -- which is why
:func:`test_normalization_strips_keywords_but_never_a_parameter_named_like_one`
exercises it on a synthetic schema instead.

Both halves of this file assert facts about the same served payload. The schema
half pins its shape;
:func:`test_the_surface_states_the_enforced_concurrency_ceiling` pins the one
documented knob whose ceiling that payload states to the calling model, across
every surface that repeats the number.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.types import Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.server import (
    _resolve_web_search_max_concurrency,
    mcp,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stands in for description text so a reword cannot fail the golden, while a
# description appearing or vanishing still can.
DESCRIPTION_SENTINEL = "<present>"


@dataclass(frozen=True)
class ToolContract:
    """One tool's advertised input schema, normalized.

    Attributes:
        name: Tool name as served to clients.
        input_schema: Expected schema after :func:`_normalize`. Carries no
            ``title`` key, because normalization drops every one.
    """

    name: str
    input_schema: dict[str, Any]


# Measured against `mcp==1.25.0` and `mcp==1.29.1` -- the floor and the newest
# release the `mcp>=1.25,<2` bound allows -- which agree exactly. No parameter
# carries a `description`: FastMCP builds the argument model from the signature
# and never reads the docstring's `Args:` block, so the only description in the
# payload is the tool-level one, asserted separately below.
TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        "web_search",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    ),
    ToolContract(
        "get_content",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    ),
)


# JSON Schema keywords whose value is a mapping keyed by *parameter name* rather
# than by keyword. Inside one of these, `title` is a parameter called "title" --
# part of the contract -- not a generated label to be stripped. `WebSearchResult`
# already has a `title` field, so the collision is live in this project rather
# than hypothetical.
SUBSCHEMA_MAPS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)


def _normalize(node: Any, *, keyed_by_name: bool = False) -> Any:
    """Strip generated titles and description wording from a JSON Schema node.

    Recurses through dictionaries and lists. Keys are emitted in sorted order so
    that a failure renders both sides in the same order and the diff shows only
    what actually differs. That buys no correctness -- Python compares mappings by
    content -- unlike sorting ``required``, whose order genuinely is incidental to
    the contract.

    Args:
        node: Any fragment of a JSON Schema -- a mapping, a list, or a scalar.
        keyed_by_name: ``True`` when ``node``'s keys are parameter names, as
            inside ``properties``. Nothing is stripped at such a level, because a
            parameter may legitimately be called ``title`` or ``description``.

    Returns:
        The same fragment with every generated ``title`` keyword removed and every
        ``description`` keyword's value replaced by :data:`DESCRIPTION_SENTINEL`.
    """
    if isinstance(node, dict):
        normalized: dict[str, Any] = {}
        for key, value in sorted(node.items()):
            # `title` is generated from the parameter name by Pydantic, and
            # description wording may be rewritten across the allowed SDK range.
            # Both are cosmetic only where the key is a schema keyword.
            if not keyed_by_name and key == "title":
                continue
            if not keyed_by_name and key == "description":
                normalized[key] = DESCRIPTION_SENTINEL
                continue
            normalized[key] = _normalize(
                value, keyed_by_name=not keyed_by_name and key in SUBSCHEMA_MAPS
            )
        if not keyed_by_name and isinstance(normalized.get("required"), list):
            normalized["required"] = sorted(normalized["required"])
        return normalized
    if isinstance(node, list):
        return [_normalize(item) for item in node]
    return node


async def _served_tool(name: str) -> Tool:
    """Return the tool of the given name as clients receive it.

    Args:
        name: Tool name to look up.

    Returns:
        The :class:`~mcp.types.Tool` FastMCP serves over ``list_tools()``.

    Raises:
        AssertionError: When no tool of that name is exposed.
    """
    served = {tool.name: tool for tool in await mcp.list_tools()}

    assert name in served, (
        f"The server no longer exposes a tool named {name!r}; it exposes "
        f"{sorted(served)}. Every MCP client binds to these names."
    )
    return served[name]


async def test_the_exposed_tool_set_is_exactly_the_documented_pair() -> None:
    """Expose the two tools clients bind to, and no others"""
    served = sorted(tool.name for tool in await mcp.list_tools())

    assert served == sorted(contract.name for contract in TOOL_CONTRACTS), (
        f"The advertised tool set changed to {served}. Adding or removing a tool "
        "is a public API change; update TOOL_CONTRACTS in the same commit."
    )


@pytest.mark.parametrize("contract", TOOL_CONTRACTS, ids=lambda c: c.name)
async def test_the_input_schema_matches_the_golden(contract: ToolContract) -> None:
    """Serve the exact parameter names, types, required-ness and defaults pinned here.

    Args:
        contract: Expected contract for the tool under test.
    """
    served = _normalize((await _served_tool(contract.name)).inputSchema)

    assert served == _normalize(contract.input_schema), (
        f"{contract.name}'s advertised input schema changed. Clients bind to "
        "parameter names, types, required-ness and defaults; any change here "
        "breaks them silently."
    )


@pytest.mark.parametrize("contract", TOOL_CONTRACTS, ids=lambda c: c.name)
async def test_the_tool_advertises_no_output_schema(contract: ToolContract) -> None:
    """Advertise no result contract, which is normative for this implementation.

    Both tools are annotated ``-> dict``, so FastMCP derives no result schema and
    clients receive none. This assertion is what fails the day someone annotates
    a tool with its response model -- a production API change with client impact,
    not a test change.

    Args:
        contract: Expected contract for the tool under test.
    """
    served = await _served_tool(contract.name)

    assert served.outputSchema is None, (
        f"{contract.name} now advertises an outputSchema. Giving clients a result "
        "contract is a deliberate API change; make it deliberately."
    )


@pytest.mark.parametrize("contract", TOOL_CONTRACTS, ids=lambda c: c.name)
async def test_the_tool_ships_a_description(contract: ToolContract) -> None:
    """Ship a non-empty description, which is the tool specification the model reads.

    Only presence is asserted. The wording is asserted by
    ``tests/test_tool_descriptions.py``, and pinning text twice would make every
    reword a two-file edit.

    Args:
        contract: Expected contract for the tool under test.
    """
    served = await _served_tool(contract.name)

    assert (served.description or "").strip(), (
        f"{contract.name} ships an empty description. The calling model has "
        "nothing but this text to decide when to use the tool."
    )


def test_normalization_strips_keywords_but_never_a_parameter_named_like_one() -> None:
    """Distinguish a generated ``title`` keyword from a parameter called ``title``.

    The live payload cannot exercise this: neither tool has a parameter named
    ``title`` or ``description``, and both SDK ends emit identical text, so the
    stripping rules have no discriminating input there. This synthetic schema
    supplies one, so removing either rule -- or making the walk blind to which
    level it is on -- fails a case.
    """
    normalized = _normalize(
        {
            "title": "web_searchArguments",
            "type": "object",
            "properties": {
                "title": {"title": "Title", "type": "string", "description": "A."},
                "description": {"title": "Description", "type": "string"},
            },
            "required": ["title", "description"],
        }
    )

    assert normalized == {
        "properties": {
            "description": {"type": "string"},
            "title": {"description": DESCRIPTION_SENTINEL, "type": "string"},
        },
        "required": ["description", "title"],
        "type": "object",
    }


def test_normalization_leaves_a_schema_without_required_unchanged_in_that_respect() -> (
    None
):
    """Tolerate the absence of ``required`` rather than raising on it.

    A tool whose every parameter has a default is served with no ``required`` key
    at all. Sorting it unconditionally would turn that contract change into a
    ``KeyError`` -- an error, not the legible golden mismatch it should be.
    """
    assert _normalize({"type": "object", "properties": {}}) == {
        "type": "object",
        "properties": {},
    }


CONCURRENCY_VARIABLE = "KINDLY_WEB_SEARCH_MAX_CONCURRENCY"

# Both far above any plausible ceiling, and the result count above the environment
# value on purpose: the resolver also bounds concurrency by `num_results`, so a
# small count returns the same answer whether the ceiling exists or not. That
# masking is exactly why this ceiling went uncovered for so long.
_CEILING_PROBE_ENVIRONMENT_VALUE = 1000
_CEILING_PROBE_RESULT_COUNT = 1001

_STATED_CEILING = re.compile(r"1\.\.`?(\d+)")


def _enforced_ceiling(monkeypatch: pytest.MonkeyPatch) -> int:
    """Return the concurrency ceiling the running code actually enforces.

    Probes the resolver rather than restating the constant. A literal in this file
    would be a third copy of the number, and comparing two documents against a
    copy catches drift but never deletion -- remove the clamp and the copy still
    agrees with both documents.

    Args:
        monkeypatch: Fixture used to set the environment variable for the probe.

    Returns:
        The largest concurrency the resolver will return.
    """
    monkeypatch.setenv(CONCURRENCY_VARIABLE, str(_CEILING_PROBE_ENVIRONMENT_VALUE))
    return _resolve_web_search_max_concurrency(_CEILING_PROBE_RESULT_COUNT)


# How many lines from an anchor the claim may sit, the anchor's own line included.
# Measured, not guessed: the tool description and the README state the ceiling on
# the anchor line itself, `.env.example` one line below the assignment, and the
# review rule three lines below the function name -- the widest gap, so 4 is that
# gap and no more. Widening it further weakens the scoping that stops another
# knob's range being read as this one's; a claim that drifts beyond it fails
# loudly as "states no ceiling" rather than passing silently.
CEILING_CLAIM_WINDOW_LINES = 4


@dataclass(frozen=True)
class CeilingSurface:
    """One place that repeats the concurrency ceiling in prose.

    Attributes:
        name: Human-readable name, used in failure messages.
        path: Repository-relative file to read, or ``None`` for the tool
            description as it is served to clients.
        anchor: Text that marks where this surface discusses the knob. Each
            surface names the knob differently -- the documents by environment
            variable, the review rule by resolver function -- so the anchor is
            per surface rather than assumed.
    """

    name: str
    path: str | None
    anchor: str


# Every surface that states the number. `.env.example` and the review rule were
# both found stating `1..5` already, so a change to the clamp that updated only
# the code, the docstring and the README would leave two copies wrong.
CEILING_SURFACES: tuple[CeilingSurface, ...] = (
    CeilingSurface("web_search's tool description", None, CONCURRENCY_VARIABLE),
    CeilingSurface("README.md", "README.md", CONCURRENCY_VARIABLE),
    CeilingSurface(".env.example", ".env.example", CONCURRENCY_VARIABLE),
    CeilingSurface(
        ".github/review/rules/mcp-server.md",
        ".github/review/rules/mcp-server.md",
        "_resolve_web_search_max_concurrency",
    ),
)


def _stated_ceilings(text: str, anchor: str) -> list[int]:
    """Return every ``1..N`` ceiling the text claims near its anchor.

    Scans a short window starting at each line containing ``anchor``, so a range
    documenting some other knob elsewhere in the file cannot be mistaken for this
    claim. The window is needed because two surfaces separate the anchor from the
    sentence carrying the number.

    Args:
        text: Document to scan.
        anchor: Text marking where the document discusses this knob.

    Returns:
        The upper bound of each ``1..N`` range found in an anchored window, in
        document order. Empty when the document states no ceiling.
    """
    lines = text.splitlines()
    return [
        int(match.group(1))
        for index, line in enumerate(lines)
        if anchor in line
        for window_line in lines[index : index + CEILING_CLAIM_WINDOW_LINES]
        for match in _STATED_CEILING.finditer(window_line)
    ]


@pytest.mark.parametrize("surface", CEILING_SURFACES, ids=lambda s: s.name)
async def test_the_surface_states_the_enforced_concurrency_ceiling(
    surface: CeilingSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State the real concurrency ceiling everywhere the number is repeated.

    The tool description is read as it is *served*, not from the source docstring,
    because the served text is what a client actually receives.

    Args:
        surface: The document or served text under test.
        monkeypatch: Fixture used to probe the resolver's enforced ceiling.

    Raises:
        AssertionError: When the surface states no ceiling, or states any value
            other than the one the code enforces.
    """
    enforced = _enforced_ceiling(monkeypatch)
    text = (
        (await _served_tool("web_search")).description or ""
        if surface.path is None
        else (REPO_ROOT / surface.path).read_text(encoding="utf-8")
    )

    stated = _stated_ceilings(text, surface.anchor)

    assert stated, (
        f"{surface.name} discusses {CONCURRENCY_VARIABLE} without stating its "
        f"ceiling. The code silently clamps to {enforced}, so a reader who sets a "
        "higher value gets no indication it was ignored."
    )
    assert set(stated) == {enforced}, (
        f"{surface.name} states a ceiling of {sorted(set(stated))} for "
        f"{CONCURRENCY_VARIABLE}; the code enforces {enforced}."
    )
