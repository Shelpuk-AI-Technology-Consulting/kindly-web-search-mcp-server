"""Pin the tool contract this server advertises to MCP clients.

FastMCP derives each tool's JSON Schema from its Python signature and its
description from its docstring, then serves both over
:meth:`~mcp.server.fastmcp.FastMCP.list_tools`. That payload is this server's
public API: renaming a parameter breaks every client, and until this file existed
nothing in the suite noticed.

The comparison is normalized before it is made. Generated ``title`` fields are
dropped and description *wording* is replaced by a sentinel, because the allowed
SDK range (``mcp>=1.25,<2``) may reorder keys or rewrite generated text in a minor
release. Nothing else is dropped: a key a future SDK adds fails the golden, which
is what a golden over a public API is for.

Both halves of this file assert facts about the same served payload. The schema
half pins its shape; :func:`test_the_tool_description_states_the_enforced_concurrency_ceiling`
and :func:`test_the_readme_states_the_enforced_concurrency_ceiling` pin the one
documented knob whose ceiling that payload states to the calling model.
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


def _normalize(node: Any) -> Any:
    """Strip generated titles and description wording from a JSON Schema node.

    Recurses through dictionaries and lists. Keys are emitted in sorted order so
    that a failure renders both sides in the same order and the diff shows only
    what actually differs; ``required`` is sorted because its order is incidental
    to the contract.

    Args:
        node: Any fragment of a JSON Schema -- a mapping, a list, or a scalar.

    Returns:
        The same fragment with every ``title`` key removed and every
        ``description`` value replaced by :data:`DESCRIPTION_SENTINEL`.
    """
    if isinstance(node, dict):
        normalized: dict[str, Any] = {}
        for key, value in sorted(node.items()):
            # `title` is generated from the parameter name by Pydantic and is
            # explicitly reorderable/rewritable across the allowed SDK range.
            if key == "title":
                continue
            normalized[key] = (
                DESCRIPTION_SENTINEL if key == "description" else _normalize(value)
            )
        if isinstance(normalized.get("required"), list):
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


def _stated_ceilings(text: str) -> list[int]:
    """Return every ``1..N`` ceiling the text claims for the concurrency variable.

    Scans line by line and keeps only lines naming the variable, so an unrelated
    range elsewhere in the document cannot be mistaken for this claim.

    Args:
        text: Document to scan.

    Returns:
        The upper bound of each ``1..N`` range stated alongside the variable, in
        document order. Empty when the document states no ceiling.
    """
    return [
        int(match.group(1))
        for line in text.splitlines()
        if CONCURRENCY_VARIABLE in line
        for match in _STATED_CEILING.finditer(line)
    ]


def _assert_states_enforced_ceiling(text: str, source: str, enforced: int) -> None:
    """Assert a document states the ceiling the code enforces, and only that one.

    Args:
        text: Document to check.
        source: Human-readable name of the document, used in failure messages.
        enforced: Ceiling measured from the running resolver.

    Raises:
        AssertionError: When the document states no ceiling, or states any value
            other than ``enforced``.
    """
    stated = _stated_ceilings(text)

    assert stated, (
        f"{source} documents {CONCURRENCY_VARIABLE} without stating its ceiling. "
        f"The code silently clamps to {enforced}, so a reader who sets a higher "
        "value gets no indication it was ignored."
    )
    assert set(stated) == {enforced}, (
        f"{source} states a 1..{sorted(set(stated))} ceiling for "
        f"{CONCURRENCY_VARIABLE}; the code enforces 1..{enforced}."
    )


async def test_the_tool_description_states_the_enforced_concurrency_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State the real concurrency ceiling in the text sent to the calling model.

    Asserts against the served description rather than the source docstring,
    because the served text is what a client actually receives.

    Args:
        monkeypatch: Fixture used to probe the resolver's enforced ceiling.
    """
    description = (await _served_tool("web_search")).description or ""

    _assert_states_enforced_ceiling(
        description, "web_search's tool description", _enforced_ceiling(monkeypatch)
    )


def test_the_readme_states_the_enforced_concurrency_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State the real concurrency ceiling where users are told to set the variable.

    Args:
        monkeypatch: Fixture used to probe the resolver's enforced ceiling.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    _assert_states_enforced_ceiling(readme, "README.md", _enforced_ceiling(monkeypatch))
