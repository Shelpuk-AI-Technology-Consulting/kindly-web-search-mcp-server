"""Cover secret masking in diagnostics env snapshots.

:func:`~kindly_web_search_mcp_server.utils.diagnostics.mask_env_values` is applied
to env snapshots emitted to stderr when diagnostics are enabled. Those snapshots
deliberately include proxy variables (see ``scrape/universal_html.py``), and a
proxy URL may carry credentials in its userinfo component, so masking has to
survive both name-based and value-based secrets.

**This module does not hold the whole stream, and one case here says so.** The
value-based rule matches ``://user:pass@``, so a variable whose name signals
nothing and whose value has no scheme is masked by neither rule.
``SEARXNG_BASE_URL`` is exactly that shape when an operator mistypes it, and
``test_a_schemeless_base_url_is_not_masked_today`` characterizes the gap rather
than hiding it -- see ``.system_design/TEST_SUITE.md`` section 14 for the
measurement and the step that owns closing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.utils.diagnostics import mask_env_values

REDACTED_USERINFO = "://***@"


def test_masks_variables_named_like_secrets() -> None:
    """Keep masking values whose variable name signals a secret"""
    masked = mask_env_values(
        {"SERPER_API_KEY": "abc123", "GITHUB_TOKEN": "ghp_xyz", "AUTH_BEARER": "b"}
    )

    assert masked["SERPER_API_KEY"] == "*** (6)"
    assert masked["GITHUB_TOKEN"] == "*** (7)"
    assert masked["AUTH_BEARER"] == "*** (1)"


def test_redacts_credentials_embedded_in_proxy_urls() -> None:
    """Strip userinfo from proxy URLs, which the name-based rule cannot catch"""
    masked = mask_env_values(
        {
            "HTTP_PROXY": "http://alice:s3cr3t@proxy.corp:8080",
            "HTTPS_PROXY": "https://alice:s3cr3t@proxy.corp:8080",
            "ALL_PROXY": "socks5://bob:hunter2@10.0.0.1:1080",
        }
    )

    for value in masked.values():
        assert "s3cr3t" not in value
        assert "hunter2" not in value
        assert "alice" not in value
        assert "bob" not in value
        assert REDACTED_USERINFO in value


def test_keeps_proxy_host_visible_for_diagnostics() -> None:
    """Preserve the host and port, which is what the snapshot exists to show"""
    masked = mask_env_values({"HTTP_PROXY": "http://alice:s3cr3t@proxy.corp:8080"})

    assert masked["HTTP_PROXY"] == "http://***@proxy.corp:8080"


def test_redacts_password_less_userinfo() -> None:
    """Cover the `user@host` form, which carries no colon to key on"""
    masked = mask_env_values({"HTTP_PROXY": "http://alice@proxy.corp:8080"})

    assert masked["HTTP_PROXY"] == "http://***@proxy.corp:8080"


def test_redacts_password_containing_unescaped_at_sign() -> None:
    """Redact through to the last `@`, so no tail of the password survives"""
    masked = mask_env_values({"HTTP_PROXY": "http://user:pa@ss@proxy.corp:8080"})

    assert masked["HTTP_PROXY"] == "http://***@proxy.corp:8080"
    assert "ss" not in masked["HTTP_PROXY"].replace("proxy.corp", "")


def test_redacts_each_url_in_a_multi_url_value() -> None:
    """Handle values holding more than one credentialed URL"""
    masked = mask_env_values({"X": "http://a:b@one.example,https://c:d@two.example"})

    assert masked["X"] == "http://***@one.example,https://***@two.example"


def test_redacts_credentials_in_any_url_valued_variable() -> None:
    """Apply userinfo redaction regardless of the variable's name"""
    masked = mask_env_values({"SEARXNG_BASE_URL": "https://u:p@searx.example.org"})

    assert masked["SEARXNG_BASE_URL"] == "https://***@searx.example.org"


def test_a_schemeless_base_url_is_not_masked_today() -> None:
    """Pin the gap rather than the fix: a schemeless value passes through whole.

    **Characterization, not enforcement.** This case asserts today's answer, and
    today's answer is a disclosure: ``mask_env_values`` masks by *name* hint --
    ``KEY``, ``TOKEN``, ``SECRET``, ``PASSWORD``, ``BEARER`` -- and
    ``SEARXNG_BASE_URL`` matches none of them, so it falls through to
    ``redact_url_credentials``, which matches ``://user:pass@`` and therefore
    cannot touch a value with no scheme. The variable carries its credential in
    the URL userinfo, and omitting the scheme is the ordinary way to mistype it,
    so the password reaches the emitted ``env`` payload on stderr verbatim.

    The sibling case above covers the *with-scheme* form and passes. The two
    together are the point: the difference between them is a scheme, and nothing
    about the variable or its value says which one an operator typed.

    **Revisit trigger.** ``.system_design/TEST_SUITE.md`` section 14 records this
    as an open gap owned by the emit-boundary step (E9-1), because every candidate
    repair changes what ``mask_env_values`` does -- which is the behaviour E5-7 is
    chartered to pin as-is and the policy E9-1 has yet to settle. When that step
    lands, **this case is meant to fail**, and its failure is the good outcome:
    delete it and assert the masking instead.

    It is scoped to what was measured. It does not claim this is the only
    unmasked shape, only that this one is unmasked.
    """
    leaked = "operator:notarealpassword@searx.example.org"

    masked = mask_env_values({"SEARXNG_BASE_URL": leaked})

    assert masked["SEARXNG_BASE_URL"] == leaked
    assert REDACTED_USERINFO not in masked["SEARXNG_BASE_URL"]


def test_leaves_ordinary_values_untouched() -> None:
    """Pass through values that carry no credentials"""
    masked = mask_env_values(
        {
            "NO_PROXY": "localhost,127.0.0.1",
            "HTTP_PROXY": "http://proxy.corp:8080",
            "LOG_LEVEL": "INFO",
            "SEARXNG_BASE_URL": "https://searx.example.org",
            # An `@` outside the authority is not userinfo and must survive intact.
            "SOME_URL": "https://example.org/paths/a@b",
        }
    )

    assert masked["NO_PROXY"] == "localhost,127.0.0.1"
    assert masked["HTTP_PROXY"] == "http://proxy.corp:8080"
    assert masked["LOG_LEVEL"] == "INFO"
    assert masked["SEARXNG_BASE_URL"] == "https://searx.example.org"
    assert masked["SOME_URL"] == "https://example.org/paths/a@b"
