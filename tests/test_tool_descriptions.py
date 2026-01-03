from __future__ import annotations

import re


def _doc(obj: object) -> str:
    doc = getattr(obj, "__doc__", None) or ""
    return doc.strip()


def _count_bullets_in_section(doc: str, header: str) -> int:
    """
    Count '-' bullet lines inside a docstring section starting at `header`.

    This is intentionally heuristic: the goal is to enforce that we provide multiple
    concrete examples without asserting exact phrasing.
    """
    match = re.search(
        rf"^\s*{re.escape(header)}\s*:\s*$",
        doc,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return 0
    after = doc[match.end() :]
    # Stop at the next section header-like line (e.g., "When not to use:", "Args:", "Returns:", etc.).
    stop = re.search(
        r"^\s*[A-Z][A-Za-z _/-]{2,}\s*:\s*$",
        after,
        flags=re.MULTILINE,
    )
    body = after[: stop.start()] if stop else after
    return len(re.findall(r"^\s*-\s+\S", body, flags=re.MULTILINE))


def test_web_search_tool_docstring_is_agent_oriented() -> None:
    from kindly_web_search_mcp_server.server import web_search

    doc = _doc(web_search)
    assert doc, "web_search must have a non-empty docstring (tool description)."

    # Multiple concrete “when to use” examples (agent-facing guidance).
    assert _count_bullets_in_section(doc, "When to use") >= 2

    # Explicit “when not to use” and cross-reference to get_content.
    assert re.search(r"when not to use", doc, flags=re.IGNORECASE)
    assert re.search(r"\bget_content\b", doc)

    # Env vars in a configuration/prerequisites context (not just mentioned).
    assert re.search(r"(requires|prereq|config).{0,200}\bSERPER_API_KEY\b", doc, flags=re.IGNORECASE | re.DOTALL)
    assert re.search(r"(requires|prereq|config).{0,200}\bTAVILY_API_KEY\b", doc, flags=re.IGNORECASE | re.DOTALL)

    # num_results default + recommended range (context control).
    assert re.search(r"\bnum_results\b.*\bdefault\b", doc, flags=re.IGNORECASE | re.DOTALL)
    assert re.search(r"\bnum_results\b.*\brecommended\b.*\brange\b", doc, flags=re.IGNORECASE | re.DOTALL)

    # Output shape and page_content guarantees.
    assert re.search(r"results", doc)
    assert re.search(r"page_content", doc)
    assert re.search(r"page_content.*always.*string", doc, flags=re.IGNORECASE | re.DOTALL)


def test_get_content_tool_docstring_is_agent_oriented() -> None:
    from kindly_web_search_mcp_server.server import get_content

    doc = _doc(get_content)
    assert doc, "get_content must have a non-empty docstring (tool description)."

    assert _count_bullets_in_section(doc, "When to use") >= 2
    assert re.search(r"when not to use", doc, flags=re.IGNORECASE)
    assert re.search(r"\bweb_search\(", doc)

    assert re.search(r"\"url\"\s*:\s*str", doc)
    assert re.search(r"\"page_content\"\s*:\s*str", doc)
    assert re.search(r"page_content.*always.*string", doc, flags=re.IGNORECASE | re.DOTALL)
    assert re.search(r"\b(PDF|unsupported)\b", doc, flags=re.IGNORECASE)
