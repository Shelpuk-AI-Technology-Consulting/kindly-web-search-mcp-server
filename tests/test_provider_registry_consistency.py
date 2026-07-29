"""Hold every provider list in the tree in sync with the registry.

Providers used to be enumerated separately in the router, the startup preflight
check, the ``web_search`` tool description, ``.env.example`` and the README. Two
providers were added without updating all five, so a SerpBase-only or Sofya-only
install was told no provider was configured while search worked fine.

:data:`~kindly_web_search_mcp_server.search.PROVIDERS` is now the single source of
truth. The prose copies cannot be generated, so these tests fail when one drifts.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.search import PROVIDERS, SearchProviderSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool_docstring() -> str:
    """Return the ``web_search`` tool description shown to the model.

    Returns:
        The docstring registered as the tool's description.
    """
    from kindly_web_search_mcp_server.server import web_search

    return getattr(web_search, "__doc__", "") or ""


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_tool_description_documents_every_provider(
    provider: SearchProviderSpec,
) -> None:
    """Name every provider in the description the model reads.

    Args:
        provider: Registry entry under test.
    """
    doc = _tool_docstring()

    assert provider.env_var in doc, (
        f"web_search's docstring omits {provider.env_var}. It is the tool "
        "description the model uses to decide how to configure search."
    )
    assert provider.label in doc, f"web_search's docstring omits {provider.label}."


def _assert_states_registry_order(text: str, pattern: str, source: str) -> None:
    """Assert the sentence matched by ``pattern`` lists providers in registry order.

    Checks the matched sentence alone rather than the surrounding document. A
    document-wide scan passes as soon as any earlier list happens to be ordered,
    which would leave the ordering claim itself unchecked.

    Args:
        text: Document containing the ordering claim.
        pattern: Regex whose first group captures the claim, on one line.
        source: Human-readable name of the document, used in failure messages.
    """
    match = re.search(pattern, text)
    assert match, f"{source} no longer states a provider order."

    claim = match.group(1)
    positions = [claim.find(provider.label) for provider in PROVIDERS]
    missing = [p.label for p, pos in zip(PROVIDERS, positions) if pos < 0]

    assert not missing, f"{source} order omits {missing}: {claim.strip()}"
    assert positions == sorted(positions), (
        f"{source} order '{claim.strip()}' disagrees with PROVIDERS: "
        f"{[p.name for p in PROVIDERS]}."
    )


def test_tool_description_states_the_real_routing_order() -> None:
    """Describe providers in the order the router actually selects them"""
    _assert_states_registry_order(
        _tool_docstring(),
        r"Provider routing \(strict order\):([^\n]*)",
        "web_search's docstring",
    )


def test_env_example_states_the_real_selection_order() -> None:
    """Keep the example env file's stated selection order honest"""
    _assert_states_registry_order(
        (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        r"Selection order is:([^\n]*)",
        ".env.example",
    )


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_env_example_documents_every_provider(provider: SearchProviderSpec) -> None:
    """Offer every provider's variable in the shipped example env file.

    Args:
        provider: Registry entry under test.
    """
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert f"{provider.env_var}=" in env_example, (
        f".env.example has no {provider.env_var} entry."
    )


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_readme_documents_every_provider(provider: SearchProviderSpec) -> None:
    """Mention every provider in the README.

    Args:
        provider: Registry entry under test.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert provider.env_var in readme, f"README.md never mentions {provider.env_var}."
    assert provider.label in readme, f"README.md never mentions {provider.label}."


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_no_startup_warning_for_any_single_provider(
    provider: SearchProviderSpec, monkeypatch
) -> None:
    """Start quietly when exactly one provider is configured, whichever it is.

    This is the defect behind the issue: the preflight check named three providers
    explicitly, so a SerpBase-only or Sofya-only install was warned that search
    would fail while it worked normally.

    Args:
        provider: Registry entry to configure as the only provider.
        monkeypatch: pytest fixture used to scope environment changes.
    """
    from kindly_web_search_mcp_server.server import _provider_configuration_warning

    for entry in PROVIDERS:
        monkeypatch.delenv(entry.env_var, raising=False)
    monkeypatch.setenv(provider.env_var, "configured")

    assert _provider_configuration_warning() is None


def test_startup_warning_lists_every_provider_when_none_configured(
    monkeypatch,
) -> None:
    """Name every option when nothing is configured.

    Args:
        monkeypatch: pytest fixture used to scope environment changes.
    """
    from kindly_web_search_mcp_server.server import _provider_configuration_warning

    for entry in PROVIDERS:
        monkeypatch.delenv(entry.env_var, raising=False)

    warning = _provider_configuration_warning()

    assert warning is not None
    for entry in PROVIDERS:
        assert entry.env_var in warning


def test_readme_provider_lists_are_complete() -> None:
    """Complete every README line that enumerates provider variables

    A line naming two or more provider variables is claiming to list the options,
    so it must name them all. Lines naming exactly one are configuration snippets
    (a shell export, a JSON key, a ``-e`` flag) and are left alone. Checking only
    that each provider appears *somewhere* in the README misses a list that was
    updated in one place and skipped in another.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    env_vars = {provider.env_var for provider in PROVIDERS}

    incomplete = []
    for number, line in enumerate(readme.splitlines(), start=1):
        present = {env_var for env_var in env_vars if env_var in line}
        if len(present) >= 2 and present != env_vars:
            incomplete.append((number, sorted(env_vars - present), line.strip()[:70]))

    assert not incomplete, (
        "README lines enumerate providers incompletely:\n"
        + "\n".join(
            f"  L{number} missing {missing}: {text}"
            for number, missing, text in incomplete
        )
    )


def test_registry_entries_are_unique() -> None:
    """Keep provider names, variables and functions distinct"""
    for field in ("name", "env_var", "function_name", "diagnostics_key"):
        values = [getattr(provider, field) for provider in PROVIDERS]
        assert len(values) == len(set(values)), f"duplicate {field} in PROVIDERS"


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
def test_registry_resolves_each_search_function(provider: SearchProviderSpec) -> None:
    """Resolve a callable for every registry entry.

    Args:
        provider: Registry entry under test.
    """
    assert callable(provider.search_function())
