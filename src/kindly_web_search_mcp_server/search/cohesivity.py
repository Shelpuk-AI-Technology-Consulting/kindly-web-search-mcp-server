"""Cohesivity search provider.

Queries Cohesivity search and maps its ``results`` onto
:class:`~kindly_web_search_mcp_server.models.WebSearchResult`. It is the last
entry in :data:`~kindly_web_search_mcp_server.search.PROVIDERS`, so it serves a
query only when ``COHESIVITY_APPLICATION_KEY`` is the one provider variable
configured.

**Two modes, chosen by the variable's value.** A real application key
(``coh_app_...``, or any value other than ``auto``) is used as given: one search
request, no files read or written, no other Cohesivity call. The value ``auto``
(case-insensitive) is zero-setup mode, which finds credentials itself, first hit
wins:

1. a project ``.cohesivity`` file in the working directory or a parent, stopping
   at the user's home directory or the filesystem root -- read, never modified;
2. this server's own state file, :func:`state_file_path`;
3. otherwise a new anonymous Cohesivity project, created and provisioned for
   search through Cohesivity's public hosted MCP endpoint and saved to (2).

In ``auto`` mode the provider still reads no provider variable but its own; to
locate (2) it reads the platform's config-directory variables
(``XDG_CONFIG_HOME``, ``APPDATA``) and the home directory, which say where
files go rather than which provider is selected.

**The application key travels in the request URL**, as the ``key`` query
parameter, because that is the only form the service accepts: it rejects the key
in an ``Authorization`` header, an ``X-Api-Key`` header and the request body.
That makes every value derived from the request URL credential-bearing. The key
is handed to ``httpx`` through ``params=`` and nowhere else -- never formatted
into a message, a log line or a diagnostics payload -- and an ``httpx`` failure,
whose message quotes the full URL, is left to reach the router, which rebuilds
it without the URL. Catching that failure here and re-quoting it would put the
key in front of the MCP client.

**Hosted-MCP replies are secret-bearing too**: ``create_tenant`` returns both
keys. No message built here quotes any text from an MCP reply or from an edge
error body; messages are fixed text plus an HTTP status, an exception class name
or a claim link that :func:`_safe_claim_url` has validated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..models import WebSearchResult

logger = logging.getLogger(__name__)

#: The one endpoint this provider talks to. The path is fixed by the service. A
#: module constant rather than a local, so a test compares against the request
#: this code sends rather than a copy of it.
SEARCH_ENDPOINT = "https://cohesivity.ai/edge/exa-api/search"

#: Search mode sent with every request. ``auto`` is one of the modes an
#: anonymous tenant is allowed; others answer HTTP 403.
SEARCH_TYPE = "auto"

#: Sentences per highlight. Without a ``contents`` request the service returns
#: only an id, a title and a URL, so highlights are what supply the snippet.
HIGHLIGHT_SENTENCES = 2

#: Longest snippet kept. Highlights are extracted page text rather than a
#: search-engine snippet, so they are bounded here instead of trusting the
#: service to keep them short.
SNIPPET_MAX_CHARS = 500

#: The ``COHESIVITY_APPLICATION_KEY`` value that selects zero-setup mode.
AUTO_VALUE = "auto"

#: Cohesivity's public hosted MCP endpoint, used only in ``auto`` mode. It needs
#: no auth and no session: a bare JSON-RPC ``tools/call`` is answered directly.
MCP_ENDPOINT = "https://cohesivity.ai/mcp"

#: Upper bound on one hosted-MCP call, in seconds.
MCP_TIMEOUT_SECONDS = 30

#: Sent on hosted-MCP calls. The endpoint's firewall can reject default client
#: user agents, so this one is explicit.
MCP_USER_AGENT = "kindly-web-search-mcp-server"

#: The Cohesivity resource that search requires; provisioned once per tenant.
SEARCH_RESOURCE = "exa-api"

#: Name of the project credentials file written by Cohesivity's own tooling.
PROJECT_FILE_NAME = ".cohesivity"

#: Directory, under the platform config directory, that holds this server's state.
STATE_DIR_NAME = "kindly-web-search"

#: The state file this server owns in ``auto`` mode.
STATE_FILE_NAME = "cohesivity-tenant.json"

#: The only fields the state file stores, in the order written.
STATE_FIELDS = ("tenant_id", "coh_management_key", "coh_application_key", "expires_at")

#: The only host a claim link may point at.
CLAIM_URL_HOST = "cohesivity.ai"

#: Longest claim link quoted into an error message.
CLAIM_URL_MAX_LENGTH = 200

#: Serialises tenant creation and replacement, so concurrent searches in one
#: server create at most one tenant. Whoever holds it re-reads the state file
#: before creating anything.
_BOOTSTRAP_LOCK = asyncio.Lock()

#: Counts finished tenant-creation attempts in this process, successful or not.
#: A search that waited on :data:`_BOOTSTRAP_LOCK` while another one tried to
#: create a tenant sees the count move; if no tenant was saved, that attempt
#: failed, and the waiter fails too instead of calling ``create_tenant`` again --
#: which would be an automatic retry of a creation whose outcome may be unknown.
_bootstrap_attempts = 0

#: Whether this is Windows, where the state file lives under ``%APPDATA%`` and
#: POSIX permission bits do not apply.
_WINDOWS = os.name == "nt"

#: Makes opening the state file fail on a symlink that appeared after it was
#: checked. POSIX only; Windows has no such flag, and none is needed there.
_O_NOFOLLOW: int = 0 if _WINDOWS else os.O_NOFOLLOW


class CohesivityError(RuntimeError):
    """Report a Cohesivity response this provider cannot turn into results."""


class CohesivityConfigError(CohesivityError):
    """Report that Cohesivity was called without a usable ``COHESIVITY_APPLICATION_KEY``."""


class CohesivityBootstrapError(CohesivityError):
    """Report a failed call to Cohesivity's hosted MCP endpoint in ``auto`` mode.

    Attributes:
        ambiguous: ``True`` when the call's outcome is unknown -- a timeout, a
            network error, a 5xx or an unreadable reply -- as opposed to a
            definite refusal. Neither kind is retried automatically.
    """

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        """Build the error.

        Args:
            message: Fixed text that names the failed call; never text taken
                from the reply.
            ambiguous: Whether the call's outcome is unknown.
        """
        super().__init__(message)
        self.ambiguous = ambiguous


class CohesivityAllowanceError(CohesivityError):
    """Report that a Cohesivity project's search allowance is used up or paused.

    Raised in place of the edge's 403 or 429 in ``auto`` mode, so the agent is
    told what to ask the user to do -- open the one-click claim link -- rather
    than given a bare status. The link is quoted only after
    :func:`_safe_claim_url` accepts it.
    """


class CohesivityStateError(CohesivityError):
    """Report that the ``auto``-mode state file cannot be used safely."""


@dataclass(frozen=True)
class _Credentials:
    """One Cohesivity tenant's credentials and where they came from.

    Attributes:
        tenant_id: The tenant's id.
        application_key: The ``coh_app_`` key the search endpoint takes.
        management_key: The management key the hosted MCP tools take, when known.
        expires_at: ISO-8601 expiry of an anonymous tenant, when known.
        source: ``"project"`` for a project ``.cohesivity`` file, which is never
            modified or replaced, or ``"state"`` for this server's state file.
    """

    tenant_id: str
    application_key: str
    management_key: str | None
    expires_at: str | None
    source: str

    def __repr__(self) -> str:
        """Describe the credentials without either key.

        Returns:
            A representation naming the tenant and the source only.
        """
        return f"_Credentials(tenant_id={self.tenant_id!r}, source={self.source!r})"


def _get_cohesivity_application_key() -> str:
    """Read the Cohesivity application key from the environment.

    Returns:
        The key, with surrounding whitespace removed.

    Raises:
        CohesivityConfigError: If ``COHESIVITY_APPLICATION_KEY`` is unset, empty,
            or only whitespace.
    """
    key = os.environ.get("COHESIVITY_APPLICATION_KEY", "").strip()
    if not key:
        raise CohesivityConfigError(
            "COHESIVITY_APPLICATION_KEY is not set. Configure it as an environment "
            "variable in your IDE/run configuration."
        )
    return key


def _snippet_from(item: dict[str, Any]) -> str:
    """Build a snippet from a result's highlights.

    Highlights are joined with single spaces, runs of whitespace (page text
    often carries newlines and indentation) are collapsed, and the result is
    capped at :data:`SNIPPET_MAX_CHARS`. Entries that are not strings are
    skipped rather than failing the result.

    Args:
        item: One entry of the response's ``results`` list.

    Returns:
        The snippet, or ``""`` when the result carries no usable highlight.
    """
    highlights = item.get("highlights")
    if not isinstance(highlights, list):
        return ""

    snippet = " ".join(
        " ".join(text.split()) for text in highlights if isinstance(text, str)
    ).strip()
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[: SNIPPET_MAX_CHARS - 1].rstrip() + "…"
    return snippet


async def _post_search(
    client: httpx.AsyncClient, key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Send the search request and decode its JSON object.

    Args:
        client: The client to send the request with.
        key: The application key, sent as the ``key`` query parameter only.
        payload: The JSON body.

    Returns:
        The decoded response body.

    Raises:
        CohesivityError: If the body is not valid JSON or not a JSON object.
        httpx.HTTPError: If the request fails or Cohesivity answers with an
            error status.
    """
    # `params=` is the only place the key goes; see the module docstring.
    resp = await client.post(SEARCH_ENDPOINT, params={"key": key}, json=payload)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise CohesivityError("Cohesivity response was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise CohesivityError("Cohesivity response was not a JSON object.")
    return data


def _parse_results(data: dict[str, Any], num_results: int) -> list[WebSearchResult]:
    """Map a search response onto results.

    Args:
        data: The decoded response body.
        num_results: Maximum number of results to return.

    Returns:
        At most ``num_results`` results, in the order Cohesivity ranked them.

    Raises:
        CohesivityError: If there is no ``results`` list, or it holds results
            none of which could be parsed.
    """
    raw = data.get("results")
    if not isinstance(raw, list):
        raise CohesivityError("Cohesivity response missing `results` list.")

    results: list[WebSearchResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        link = item.get("url")
        if not isinstance(title, str) or not isinstance(link, str):
            continue

        # `page_content` is populated later by the MCP tool (best-effort).
        results.append(
            WebSearchResult(
                title=title, link=link, snippet=_snippet_from(item), page_content=""
            )
        )
        if len(results) >= num_results:
            break

    # Discarding every result means the response did not match the shape expected
    # here. Returning an empty list would be indistinguishable from "no matches"
    # and would hide the mismatch, so surface it instead.
    if raw and not results:
        raise CohesivityError(
            f"Cohesivity returned {len(raw)} result(s) but none could be parsed; "
            "each needs a string `title` and `url`. The response schema may have "
            "changed."
        )

    return results


# --------------------------------------------------------------------------
# Credential files (auto mode only).
# --------------------------------------------------------------------------


def _parse_key_values(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines, ignoring blanks and ``#`` comments.

    Args:
        text: The file's contents.

    Returns:
        The fields, with surrounding whitespace and matching quotes removed.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[name.strip()] = value
    return fields


def _usable_key(value: object) -> str | None:
    """Return ``value`` if it can be sent as a key, else ``None``.

    Args:
        value: A candidate key from a file or an MCP reply.

    Returns:
        The key when it is a non-empty string without whitespace.
    """
    if isinstance(value, str) and value and not any(c.isspace() for c in value):
        return value
    return None


def _optional_text(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``.

    Args:
        value: A candidate field value.

    Returns:
        The string, or ``None``.
    """
    return value if isinstance(value, str) and value else None


def _home_directory() -> Path | None:
    """Return the user's home directory, or ``None`` when it cannot be found.

    Returns:
        The home directory.
    """
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def _find_project_credentials() -> _Credentials | None:
    """Find a project ``.cohesivity`` file in the working directory or a parent.

    The walk stops after the user's home directory or at the filesystem root. A
    file without a usable ``coh_application_key`` is passed over. The file is
    only ever read.

    Returns:
        The first usable file's credentials, or ``None``.
    """
    try:
        cwd = Path.cwd()
    except OSError:
        return None
    home = _home_directory()
    stops = {home, home.resolve()} if home is not None else set()

    for directory in (cwd, *cwd.parents):
        candidate = directory / PROJECT_FILE_NAME
        try:
            text = (
                candidate.read_text(encoding="utf-8") if candidate.is_file() else None
            )
        except (OSError, UnicodeDecodeError):
            text = None
        if text is not None:
            fields = _parse_key_values(text)
            key = _usable_key(fields.get("coh_application_key"))
            tenant_id = _optional_text(fields.get("tenant_id"))
            if key is not None:
                return _Credentials(
                    tenant_id=tenant_id or "",
                    application_key=key,
                    management_key=_usable_key(fields.get("coh_management_key")),
                    expires_at=_optional_text(fields.get("expires_at")),
                    source="project",
                )
        if directory in stops:
            break
    return None


def state_file_path() -> Path:
    """Return where ``auto`` mode keeps the tenant it created.

    ``%APPDATA%\\kindly-web-search\\cohesivity-tenant.json`` on Windows;
    elsewhere ``$XDG_CONFIG_HOME/kindly-web-search/cohesivity-tenant.json``,
    falling back to ``~/.config`` when ``XDG_CONFIG_HOME`` is unset or relative,
    as the XDG specification requires.

    Returns:
        The state file's path. It need not exist.
    """
    home = _home_directory() or Path(".")
    if _WINDOWS:
        base = os.environ.get("APPDATA", "").strip() or str(
            home / "AppData" / "Roaming"
        )
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = xdg if xdg and os.path.isabs(xdg) else str(home / ".config")
    return Path(base) / STATE_DIR_NAME / STATE_FILE_NAME


def _refuse_symlink(path: Path, what: str) -> os.stat_result | None:
    """Stat ``path`` without following it, refusing a symbolic link.

    Args:
        path: The path to check.
        what: How the path is described in an error message.

    Returns:
        The ``lstat`` result, or ``None`` when nothing exists there.

    Raises:
        CohesivityStateError: If ``path`` is a symbolic link, or cannot be
            examined.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CohesivityStateError(
            f"Cohesivity search: cannot examine the {what} at {path} ({type(exc).__name__})."
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise CohesivityStateError(
            f"Cohesivity search: refusing to use the {what} at {path} because it "
            "is a symbolic link. Remove it and search again."
        )
    return info


def _read_state() -> _Credentials | None:
    """Read this server's state file.

    Returns:
        The saved credentials, or ``None`` when there is no usable file. An
        unreadable or malformed file counts as absent and is overwritten by the
        next bootstrap.

    Raises:
        CohesivityStateError: If the state file or its directory is a symbolic
            link, or the file is not a regular file.
    """
    path = state_file_path()
    _refuse_symlink(path.parent, "Cohesivity state directory")
    info = _refuse_symlink(path, "Cohesivity state file")
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise CohesivityStateError(
            f"Cohesivity search: the Cohesivity state file at {path} is not a regular file."
        )

    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        logger.warning("Ignoring an unreadable Cohesivity state file at %s.", path)
        return None

    if not isinstance(data, dict):
        logger.warning("Ignoring a malformed Cohesivity state file at %s.", path)
        return None
    tenant_id = _optional_text(data.get("tenant_id"))
    application_key = _usable_key(data.get("coh_application_key"))
    management_key = _usable_key(data.get("coh_management_key"))
    if tenant_id is None or application_key is None or management_key is None:
        logger.warning("Ignoring an incomplete Cohesivity state file at %s.", path)
        return None
    return _Credentials(
        tenant_id=tenant_id,
        application_key=application_key,
        management_key=management_key,
        expires_at=_optional_text(data.get("expires_at")),
        source="state",
    )


def _ensure_state_directory() -> Path:
    """Create the state directory if needed, private to the user.

    Returns:
        The state file's path.

    Raises:
        CohesivityStateError: If the directory or the file is a symbolic link,
            the directory cannot be created, or a non-directory is in its way.
    """
    path = state_file_path()
    directory = path.parent
    info = _refuse_symlink(directory, "Cohesivity state directory")
    if info is None:
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise CohesivityStateError(
                f"Cohesivity search: cannot create {directory} ({type(exc).__name__})."
            ) from None
        info = _refuse_symlink(directory, "Cohesivity state directory")
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise CohesivityStateError(
            f"Cohesivity search: {directory} is not a directory."
        )
    # The umask can widen what `makedirs` was asked for; the directory is ours.
    if not _WINDOWS and stat.S_IMODE(info.st_mode) != 0o700:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    _refuse_symlink(path, "Cohesivity state file")
    return path


def _write_state(credentials: _Credentials) -> None:
    """Save credentials to the state file atomically, readable by the user only.

    Written to a ``0600`` temporary file in the same directory and moved into
    place with :func:`os.replace`, so a reader never sees a partial file.

    Args:
        credentials: The tenant to save. Only :data:`STATE_FIELDS` are stored.

    Raises:
        CohesivityStateError: If the file cannot be written safely.
    """
    path = _ensure_state_directory()
    record = {
        "tenant_id": credentials.tenant_id,
        "coh_management_key": credentials.management_key,
        "coh_application_key": credentials.application_key,
        "expires_at": credentials.expires_at,
    }
    temp_name: str | None = None
    try:
        # `mkstemp` creates the file with O_EXCL and mode 0600.
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{STATE_FILE_NAME}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not _WINDOWS:
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        temp_name = None
    except OSError as exc:
        raise CohesivityStateError(
            f"Cohesivity search: cannot save the Cohesivity state file at {path} "
            f"({type(exc).__name__})."
        ) from None
    finally:
        if temp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)


def _discard_state() -> None:
    """Delete the state file, if it is a regular file.

    Raises:
        CohesivityStateError: If the state file is a symbolic link.
    """
    path = state_file_path()
    info = _refuse_symlink(path, "Cohesivity state file")
    if info is not None and stat.S_ISREG(info.st_mode):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _is_past(value: str | None) -> bool:
    """Report whether an ISO-8601 timestamp has passed.

    Args:
        value: The timestamp, or ``None``.

    Returns:
        ``True`` only for a parseable timestamp at or before now; a missing or
        unparseable one is treated as not expired.
    """
    if not value:
        return False
    try:
        # Python 3.11+ reads a trailing `Z` itself.
        when = datetime.fromisoformat(value)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


# --------------------------------------------------------------------------
# Cohesivity's hosted MCP endpoint (auto mode only).
# --------------------------------------------------------------------------


def _mcp_failure(
    tool: str, detail: str, *, ambiguous: bool
) -> CohesivityBootstrapError:
    """Build the error for a failed hosted-MCP call.

    Args:
        tool: The MCP tool that was called.
        detail: Fixed text describing the failure; never reply text.
        ambiguous: Whether the outcome is unknown.

    Returns:
        The error to raise.
    """
    return CohesivityBootstrapError(
        f"Cohesivity search setup failed: Cohesivity's {tool} call {detail}. "
        "It was not retried automatically; try the search again shortly.",
        ambiguous=ambiguous,
    )


def _last_sse_message(text: str) -> dict[str, Any] | None:
    """Return the last JSON-RPC message in a server-sent-events body.

    Args:
        text: The body.

    Returns:
        The last ``data:`` payload that decodes to a JSON object, or ``None``.
    """
    message: dict[str, Any] | None = None
    data_lines: list[str] = []

    def flush() -> None:
        """Decode the pending ``data:`` lines as one event, then clear them."""
        nonlocal message
        if data_lines:
            try:
                decoded = json.loads("\n".join(data_lines))
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                message = decoded
            data_lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    flush()
    return message


def _decode_mcp_body(resp: httpx.Response) -> dict[str, Any] | None:
    """Decode a hosted-MCP reply sent as JSON or as server-sent events.

    Args:
        resp: The reply.

    Returns:
        The JSON-RPC message, or ``None`` when neither form decodes.
    """
    text = resp.text
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
    return _last_sse_message(text)


def _result_text(result: dict[str, Any]) -> str:
    """Join a tool result's text blocks, for classification only -- never quoted.

    Args:
        result: The JSON-RPC ``result``.

    Returns:
        The concatenated text.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


async def _call_mcp_tool(
    client: httpx.AsyncClient, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Call one tool on Cohesivity's hosted MCP endpoint.

    Sent as a bare JSON-RPC ``tools/call`` and bounded by
    :data:`MCP_TIMEOUT_SECONDS`. ``httpx`` failures are converted here rather
    than left to the router, because this URL carries no credential and the
    caller needs to know the *setup* step failed; the message names only the
    exception class.

    Args:
        client: The client to send the request with.
        tool: The tool name.
        arguments: The tool arguments; may hold the management key.

    Returns:
        The tool's ``structuredContent``.

    Raises:
        CohesivityBootstrapError: On a transport failure, a timeout, an error
            status, an unreadable reply, a JSON-RPC error or a tool error.
    """
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "User-Agent": MCP_USER_AGENT,
    }
    try:
        resp = await asyncio.wait_for(
            client.post(
                MCP_ENDPOINT, json=request, headers=headers, timeout=MCP_TIMEOUT_SECONDS
            ),
            MCP_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise _mcp_failure(
            tool, f"did not complete ({type(exc).__name__})", ambiguous=True
        ) from None

    if resp.status_code >= 400:
        raise _mcp_failure(
            tool,
            f"failed with HTTP {resp.status_code}",
            ambiguous=resp.status_code >= 500,
        )

    message = _decode_mcp_body(resp)
    if message is None:
        raise _mcp_failure(tool, "returned an unreadable reply", ambiguous=True)

    error = message.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        detail = f" {code}" if isinstance(code, int) else ""
        raise _mcp_failure(
            tool, f"was refused (JSON-RPC error{detail})", ambiguous=False
        )

    result = message.get("result")
    if not isinstance(result, dict):
        raise _mcp_failure(tool, "returned an unreadable reply", ambiguous=True)

    if result.get("isError"):
        if "too many tenant creation" in _result_text(result).lower():
            raise _mcp_failure(
                tool, "was rate-limited (wait a minute)", ambiguous=False
            )
        raise _mcp_failure(tool, "reported an error", ambiguous=False)

    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        try:
            structured = json.loads(_result_text(result))
        except ValueError:
            structured = None
    if not isinstance(structured, dict):
        raise _mcp_failure(tool, "returned an unreadable reply", ambiguous=True)
    return structured


def _credentials_from_create(structured: dict[str, Any]) -> _Credentials:
    """Extract the new tenant's credentials from a ``create_tenant`` reply.

    Args:
        structured: The reply's ``structuredContent``.

    Returns:
        The tenant's credentials, sourced as ``"state"``.

    Raises:
        CohesivityBootstrapError: If the reply carries no usable keys.
    """
    credentials_file = structured.get("credentials_file")
    content = (
        credentials_file.get("content") if isinstance(credentials_file, dict) else None
    )
    fields = _parse_key_values(content) if isinstance(content, str) else {}
    tenant_id = _optional_text(fields.get("tenant_id")) or _optional_text(
        structured.get("tenant_id")
    )
    application_key = _usable_key(fields.get("coh_application_key"))
    management_key = _usable_key(fields.get("coh_management_key"))
    if tenant_id is None or application_key is None or management_key is None:
        raise _mcp_failure(
            "create_tenant", "returned no usable credentials", ambiguous=True
        )
    return _Credentials(
        tenant_id=tenant_id,
        application_key=application_key,
        management_key=management_key,
        expires_at=_optional_text(fields.get("expires_at"))
        or _optional_text(structured.get("expires_at")),
        source="state",
    )


async def _provision_search(
    client: httpx.AsyncClient, credentials: _Credentials
) -> None:
    """Provision search for a tenant. Idempotent on Cohesivity's side.

    Args:
        client: The client to send the request with.
        credentials: The tenant; must carry a management key.

    Raises:
        CohesivityBootstrapError: If the call fails or does not report success.
    """
    structured = await _call_mcp_tool(
        client,
        "provision_resource",
        {
            "tenant_id": credentials.tenant_id,
            "resource": SEARCH_RESOURCE,
            "confirmed": True,
            "coh_management_key": credentials.management_key,
        },
    )
    outcome = structured.get("result")
    if not isinstance(outcome, dict) or not (
        outcome.get("success") is True or outcome.get("status") == "active"
    ):
        raise _mcp_failure(
            "provision_resource", "did not report success", ambiguous=False
        )


async def _bootstrap(client: httpx.AsyncClient) -> _Credentials:
    """Create an anonymous tenant, save it, and provision search for it.

    Must be called with :data:`_BOOTSTRAP_LOCK` held. The state file is saved
    before provisioning, so a failed provisioning call is repaired by the next
    search (its 403 triggers one provisioning retry) rather than by creating a
    second tenant.

    Args:
        client: The client to send the requests with.

    Returns:
        The new tenant's credentials.

    Raises:
        CohesivityStateError: If the state file cannot be written; checked
            before the tenant is created.
        CohesivityBootstrapError: If creation or provisioning fails. Creation
            is never retried.
    """
    global _bootstrap_attempts
    _ensure_state_directory()
    try:
        structured = await _call_mcp_tool(client, "create_tenant", {"confirmed": True})
        credentials = _credentials_from_create(structured)
        _write_state(credentials)
    finally:
        # Counted once the outcome is known, so every search already waiting on
        # the lock sees it move; see `_refuse_after_concurrent_failure`.
        _bootstrap_attempts += 1
    logger.info(
        "Created anonymous Cohesivity project %s for search.", credentials.tenant_id
    )
    await _provision_search(client, credentials)
    return credentials


async def _refresh_expired(
    client: httpx.AsyncClient, saved: _Credentials
) -> _Credentials | None:
    """Decide whether a saved tenant past its ``expires_at`` is still usable.

    Must be called with :data:`_BOOTSTRAP_LOCK` held. A claimed tenant is kept,
    with its new expiry saved. An unclaimed one -- or one ``tenant_status``
    definitely refuses -- is discarded. When ``tenant_status`` cannot be
    reached, the tenant is kept and the search endpoint decides.

    Args:
        client: The client to send the request with.
        saved: The saved tenant.

    Returns:
        The credentials to use, or ``None`` when the tenant was discarded.
    """
    try:
        structured = await _call_mcp_tool(
            client,
            "tenant_status",
            {"tenant_id": saved.tenant_id, "coh_management_key": saved.management_key},
        )
    except CohesivityBootstrapError as exc:
        if exc.ambiguous:
            return saved
        _discard_state()
        return None

    status = structured.get("status")
    account = status.get("account") if isinstance(status, dict) else None
    if isinstance(account, dict) and account.get("lifecycle") == "claimed":
        refreshed = _Credentials(
            tenant_id=saved.tenant_id,
            application_key=saved.application_key,
            management_key=saved.management_key,
            expires_at=_optional_text(account.get("expires_at")),
            source="state",
        )
        _write_state(refreshed)
        return refreshed
    _discard_state()
    return None


async def _resolve_auto_credentials(
    client: httpx.AsyncClient,
) -> tuple[_Credentials, bool]:
    """Find or create the credentials ``auto`` mode searches with.

    Args:
        client: The client to send any hosted-MCP requests with.

    Returns:
        The credentials, and whether this call created the tenant.

    Raises:
        CohesivityStateError: If the state file cannot be used safely.
        CohesivityBootstrapError: If a tenant had to be created and that failed.
    """
    project = _find_project_credentials()
    if project is not None:
        return project, False

    saved = _read_state()
    if saved is not None and not _is_past(saved.expires_at):
        return saved, False

    seen_attempts = _bootstrap_attempts
    async with _BOOTSTRAP_LOCK:
        # Another search may have created or refreshed the tenant meanwhile.
        saved = _read_state()
        if saved is not None and _is_past(saved.expires_at):
            saved = await _refresh_expired(client, saved)
        if saved is not None:
            return saved, False
        _refuse_after_concurrent_failure(seen_attempts)
        return await _bootstrap(client), True


def _refuse_after_concurrent_failure(seen_attempts: int) -> None:
    """Fail a search whose wait on the lock covered a failed tenant creation.

    Must be called with :data:`_BOOTSTRAP_LOCK` held, after the state file was
    re-read and found empty.

    Args:
        seen_attempts: :data:`_bootstrap_attempts` as read before waiting.

    Raises:
        CohesivityBootstrapError: If another search tried to create a tenant
            while this one waited, and saved none.
    """
    if _bootstrap_attempts != seen_attempts:
        raise CohesivityBootstrapError(
            "Cohesivity search setup failed: a concurrent search's attempt to "
            "create the Cohesivity project failed moments ago. It was not retried "
            "automatically; try the search again shortly.",
            ambiguous=True,
        )


async def _replace_saved_tenant(
    client: httpx.AsyncClient, failed: _Credentials
) -> _Credentials:
    """Replace a saved tenant the search endpoint no longer accepts.

    Must only be called for a ``"state"`` tenant; a project file's tenant is
    never replaced.

    Args:
        client: The client to send the requests with.
        failed: The tenant that was rejected.

    Returns:
        The replacement -- one another search already saved, or a new one.

    Raises:
        CohesivityStateError: If the state file cannot be used safely.
        CohesivityBootstrapError: If creating the replacement fails.
    """
    seen_attempts = _bootstrap_attempts
    async with _BOOTSTRAP_LOCK:
        current = _read_state()
        if current is not None and current.tenant_id != failed.tenant_id:
            return current
        _refuse_after_concurrent_failure(seen_attempts)
        _discard_state()
        logger.info(
            "Replacing expired Cohesivity project %s used for search.", failed.tenant_id
        )
        return await _bootstrap(client)


def _classify_edge_failure(resp: httpx.Response) -> str | None:
    """Classify a search failure that ``auto`` mode can act on.

    Reads the error body only to classify it; nothing from it is quoted.

    Args:
        resp: The failed search response.

    Returns:
        ``"tenant_gone"`` (401, 410), ``"not_provisioned"`` (403 Service not
        provisioned), ``"paused"`` (403 tenant paused), ``"quota"`` (429 for a
        window other than the per-minute one), or ``None`` for anything else,
        a per-minute 429 included.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    message = message.lower() if isinstance(message, str) else ""
    status = resp.status_code

    if status in (401, 410):
        return "tenant_gone"
    if status == 403:
        if "service not provisioned" in message:
            return "not_provisioned"
        if body.get("tenant_state") == "paused" or "tenant paused" in message:
            return "paused"
        return None
    if status == 429:
        window = body.get("window_kind")
        if isinstance(window, str) and window and window != "utc_minute":
            return "quota"
    return None


def _safe_claim_url(candidate: object, credentials: _Credentials) -> str | None:
    """Return ``candidate`` if it is a claim link safe to quote, else ``None``.

    Accepts only an ``https`` URL on exactly :data:`CLAIM_URL_HOST`, with no
    userinfo or port, no whitespace or control characters, at most
    :data:`CLAIM_URL_MAX_LENGTH` characters, and containing neither key. The
    message it lands in is served to an LLM agent, and the value is written by
    the remote end, so anything unrecognised is dropped.

    Args:
        candidate: The ``approval_url`` from the ``claim_tenant`` reply.
        credentials: The tenant whose keys the link must not contain.

    Returns:
        The link, re-composed from its validated parts, or ``None``.
    """
    if not isinstance(candidate, str) or not candidate:
        return None
    if len(candidate) > CLAIM_URL_MAX_LENGTH:
        return None
    for secret in (credentials.application_key, credentials.management_key):
        if secret and secret in candidate:
            return None
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
        return None
    try:
        parts = urlsplit(candidate)
        has_port = parts.port is not None
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc != CLAIM_URL_HOST or has_port:
        return None
    if not parts.path.startswith("/"):
        return None
    return urlunsplit(parts)


async def _raise_allowance_error(
    client: httpx.AsyncClient, credentials: _Credentials, status: int
) -> NoReturn:
    """Raise the actionable error for a used-up or paused allowance.

    Asks ``claim_tenant`` for the one-click link that keeps the project and
    lifts the anonymous limits. No new tenant is created.

    Args:
        client: The client to send the request with.
        credentials: The tenant whose allowance is used up.
        status: The search endpoint's HTTP status.

    Raises:
        CohesivityAllowanceError: Always; with the claim link when one was
            obtained and validated.
    """
    link: str | None = None
    if credentials.management_key and credentials.tenant_id:
        try:
            structured = await _call_mcp_tool(
                client,
                "claim_tenant",
                {
                    "tenant_id": credentials.tenant_id,
                    "confirmed": True,
                    "coh_management_key": credentials.management_key,
                },
            )
        except CohesivityBootstrapError:
            structured = {}
        link = _safe_claim_url(structured.get("approval_url"), credentials)
    if link is not None:
        raise CohesivityAllowanceError(
            "Cohesivity search: the free anonymous allowance is used up. Ask the "
            f"user to open {link} to keep it (one click, free)."
        )
    raise CohesivityAllowanceError(
        f"Cohesivity search: the project's search allowance is used up or paused "
        f"(HTTP {status}), and no claim link could be fetched. Ask the user to "
        f"claim the project at https://{CLAIM_URL_HOST}."
    )


async def _search_auto(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> dict[str, Any]:
    """Run one search in ``auto`` mode, recovering where that is safe.

    At most one provisioning retry and at most one tenant creation per call. A
    project file's tenant is never replaced. Failures this cannot act on --
    a per-minute 429 among them -- leave as the original ``httpx`` error, for
    the router to convert.

    Args:
        client: The client to send every request with.
        payload: The search body.

    Returns:
        The decoded search response.

    Raises:
        CohesivityAllowanceError: If the allowance is used up or paused.
        CohesivityBootstrapError: If a hosted-MCP call fails.
        CohesivityStateError: If the state file cannot be used safely.
        CohesivityError: If the response is not a JSON object.
        httpx.HTTPError: For any other search failure.
    """
    credentials, created = await _resolve_auto_credentials(client)
    provisioned = False
    # The first search plus at most one retry after provisioning and one after
    # a replacement. The flags already bound the loop; the count makes that
    # structural, so a later edit to a flag cannot turn it into a tenant loop.
    attempts_left = 3

    while True:
        attempts_left -= 1
        try:
            return await _post_search(client, credentials.application_key, payload)
        except httpx.HTTPStatusError as exc:
            problem = _classify_edge_failure(exc.response)
            status = exc.response.status_code
            can_provision = (
                problem == "not_provisioned"
                and not provisioned
                and credentials.management_key is not None
                and bool(credentials.tenant_id)
            )
            can_replace = (
                problem == "tenant_gone"
                and credentials.source == "state"
                and not created
            )
            if problem in ("paused", "quota"):
                pass
            elif attempts_left <= 0 or not (can_provision or can_replace):
                raise
        # Acted on outside the `except` block, so nothing raised below carries
        # the key-quoting `httpx` error as its context.
        if can_provision:
            provisioned = True
            await _provision_search(client, credentials)
        elif can_replace:
            created = True
            credentials = await _replace_saved_tenant(client, credentials)
        else:
            await _raise_allowance_error(client, credentials, status)


async def search_cohesivity(
    query: str,
    *,
    num_results: int,
    http_client: httpx.AsyncClient | None = None,
) -> list[WebSearchResult]:
    """Query Cohesivity search and return parsed results.

    Cohesivity endpoint:

    - ``POST`` :data:`SEARCH_ENDPOINT`
    - Query parameter: ``key=<coh_application_key>``
    - JSON body: ``{"query": "<query>", "numResults": <num_results>,
      "type": "auto", "contents": {"highlights": {"numSentences": 2}}}``

    ``COHESIVITY_APPLICATION_KEY=auto`` selects zero-setup mode; any other value
    is sent as the key. See the module docstring.

    ``numResults`` is forwarded as given; the returned list is capped at
    ``num_results`` here instead. A result without a string ``title`` and
    ``url`` is dropped; one without highlights is kept with an empty snippet.

    Docs: https://cohesivity.ai

    Args:
        query: The search query. A blank query returns no results without a
            request.
        num_results: Maximum number of results to return. A value below 1
            returns no results without a request.
        http_client: Client to send the request with. A short-lived client with
            a 30-second timeout is created when omitted.

    Returns:
        At most ``num_results`` results, in the order Cohesivity ranked them,
        each with an empty ``page_content``.

    Raises:
        CohesivityConfigError: If ``COHESIVITY_APPLICATION_KEY`` is not usable.
        CohesivityAllowanceError: In ``auto`` mode, if the project's allowance is
            used up or paused; carries the one-click claim link when available.
        CohesivityBootstrapError: In ``auto`` mode, if a call to Cohesivity's
            hosted MCP endpoint fails.
        CohesivityStateError: In ``auto`` mode, if the state file cannot be used
            safely.
        CohesivityError: If the response is not a JSON object, has no
            ``results`` list, or holds results none of which could be parsed.
        httpx.HTTPError: If the request fails or Cohesivity answers with an
            error status -- 401 for a rejected key, 403 when search is not
            provisioned for the tenant, 429 when rate-limited. Its message
            quotes the request URL, and so the key; the router converts it so
            neither reaches the MCP client.
    """
    if not query.strip():
        return []

    if num_results < 1:
        return []

    key = _get_cohesivity_application_key()
    auto = key.lower() == AUTO_VALUE
    payload = {
        "query": query,
        "numResults": int(num_results),
        "type": SEARCH_TYPE,
        "contents": {"highlights": {"numSentences": HIGHLIGHT_SENTENCES}},
    }

    async def _run(client: httpx.AsyncClient) -> dict[str, Any]:
        """Search with the configured key, or in ``auto`` mode.

        Args:
            client: The client to send every request with.

        Returns:
            The decoded search response.
        """
        if auto:
            return await _search_auto(client, payload)
        return await _post_search(client, key, payload)

    if http_client is None:
        async with httpx.AsyncClient(timeout=30) as client:
            data = await _run(client)
    else:
        data = await _run(http_client)

    return _parse_results(data, num_results)
