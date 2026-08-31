"""Materialise the Kitty Bridge configuration and launcher on the runner.

Kitty Bridge is the single writer of the Claude CLI's environment: it resolves
the provider from ``~/.config/kitty/profiles.json``, the credential from
``credentials.json`` and the egress rules from ``egress.json``, then overrides
the child process's endpoint and credential variables to point at its local
bridge. This module puts those three files on disk from organisation-level
repository settings and writes the launcher that puts ``kitty`` in front of
``claude`` -- the ticket's "the only change is the ``kitty`` command before
``claude``".

The launcher also asks kitty for both of its logs: ``--debug-file`` for the
bridge's own record once it is up, and a ``tee`` on stderr for the launch
failures kitty prints nowhere else. Neither is decoration -- between them they
are the only evidence a failed review leaves, and without them a launch refusal
arrives at the interpreter as an empty execution record. See
:func:`wrapper_body` for which window each one covers.

The three settings reach this script through ``env:`` under neutral names,
never through ``${{ }}`` interpolation, so their values cannot appear in a
workflow log or a process listing. ``credentials.json`` is JSON with the api
keys base64-encoded inside them, not encrypted: nothing here echoes file
content, on any path, for exactly that reason -- a missing setting is
reported by name and an unexpected error by exception class, never by value.

Failure is a report, never an exit code. A missing setting, a value that is
not valid JSON, or an unexpected exception all produce ``available=false``,
a ``::error::`` annotation GitHub surfaces on the check page without opening
raw logs, exit 0, and **no** ``wrapper_path`` output -- a wrapper path beside
``available=false`` would invite the review step to launch a kitty that was
never installed. The workflow resolves the failed configuration as ``fatal``
and posts the notice. Only ``KeyboardInterrupt`` and ``SystemExit`` escape
this contract: they are runner-cancellation signals, not configuration
failures, and must propagate.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

# (environment variable, file name under the kitty config directory). The
# variable names are the workflow's neutral binding names; the file names are
# kitty's own -- it looks for exactly these three.
KITTY_CONFIG_INPUTS = (
    ("KITTY_PROFILES_JSON", "profiles.json"),
    ("KITTY_CREDENTIALS_JSON", "credentials.json"),
    ("KITTY_EGRESS_JSON", "egress.json"),
)

# The launcher handed to the review step. The basename must not be "claude":
# the action puts this directory on PATH and kitty probes PATH first, so a
# wrapper named "claude" would make kitty launch itself. --no-validate skips
# kitty's pre-flight credential check: in CI the run itself is the validation,
# and a transient blip at the provider's auth endpoint must surface inside the
# run (a classified credential error) rather than as an empty execution record
# that resolves to "re-running will not fix this".
WRAPPER_BASENAME = "kitty-claude-launcher"

#: Kitty's two logs, both under ``RUNNER_TEMP`` so the workflow can find them
#: without this script announcing where they are. They are per-RUNNER, not
#: per-run: the workflow purges stale copies before it launches, so at most one
#: failed run's evidence is ever on a machine.
BRIDGE_DEBUG_LOG = "kitty-bridge-debug.log"
BRIDGE_STDERR_LOG = "kitty-bridge-stderr.log"

# Two separate captures, because they cover two disjoint windows and a failure
# in either one was previously invisible.
#
# ``2> >(tee -a ...)`` catches kitty's LAUNCH stderr. Kitty prints its own
# failures -- an egress refusal, a profile whose credential will not resolve --
# to stderr and nowhere else, and `claude-code-action` writes an execution
# record carrying none of it. That is how a launch failure reached
# `interpret_claude_result.py` as an empty record and came out as "no execution
# record; Claude never reached the model" -- true, and useless.
#
# ``--debug-file`` covers everything AFTER the bridge is up, which the tee
# cannot see: kitty goes quiet on stderr once it is running, so a run that
# reaches the model and then stalls leaves an empty stderr log and no execution
# record at all. Verified against kitty's own CLI source
# (``src/kitty/cli/main.py``): ``--debug-file PATH`` writes there "instead of
# ~/.cache/kitty/bridge.log" and "implies --debug". The explicit path is the
# point -- the bare ``--debug`` form writes under ``~/.cache``, where this
# workflow neither finds it nor purges it.
#
# 🔴 The debug log carries kitty's inbound request bodies: the bridge token and
# the entire review prompt. It must never be uploaded as an artifact. The
# workflow keeps it on the runner when the model step fails and deletes it when
# it succeeds; a filtered timeline is what travels.
#: The environment variable ``actions/setup-python`` exports into ``$GITHUB_ENV``. It
#: names the interpreter the job provisioned, and therefore the ``bin`` directory pip
#: installs console scripts into.
PYTHON_LOCATION = "pythonLocation"


def wrapper_body(kitty_bin: str) -> str:
    """Render the launcher, addressing kitty by an absolute path.

    🔴 **upstream: ``exec kitty`` stood here and resolved through ``PATH``.** ``kitty`` is
    a pip console script, so it lands beside the interpreter that installed it -- and on
    this fleet ``PATH`` is not reliably that interpreter's ``bin``: a stale tool-cache
    entry can precede it, and ``$HOME`` (hence ``~/.local/bin``) persists between jobs on
    a self-hosted runner. This wrapper is what ``claude-code-action`` runs as
    ``path_to_claude_code_executable``, so a ``kitty`` resolved to the wrong copy -- or to
    none -- surfaces as ``exit 127`` inside the action, which
    ``interpret_claude_result.py`` reports as ``fatal -- no execution record``. That
    verdict points at the provider and names nothing about ``PATH``.

    ``scripts/check_workflow_python.py`` cannot see this: its corpus is workflow and
    action YAML, and this launcher is generated. The test beside it is what holds the
    rule here.

    Args:
        kitty_bin: Absolute path to the ``kitty`` console script.

    Returns:
        The launcher's full text, including its shebang.
    """
    return (
        "#!/usr/bin/env bash\n"
        f'exec "{kitty_bin}" --no-validate --debug-file "${{RUNNER_TEMP:-/tmp}}/'
        + BRIDGE_DEBUG_LOG
        + '" \\\n'
        '  claude "$@" 2> >(tee -a "${RUNNER_TEMP:-/tmp}/'
        + BRIDGE_STDERR_LOG
        + '" >&2)\n'
    )


def _egress_record(document: object) -> dict:
    """Return the stored gateway record from an ``egress.json`` document.

    Kitty wraps the record in a versioned envelope --
    ``{"version": 1, "egress": {…}}`` -- and reads it back through
    ``EgressStore.load`` → ``EgressRecord.from_dict`` (``kitty-bridge`` 1.5.0,
    ``src/kitty/egress_store.py:145-149`` and ``:78-82``). Every field of the gateway,
    ``proxy_url`` and ``auth_ref`` included, is inside that inner object.

    Args:
        document: The parsed ``egress.json``, of any shape.

    Returns:
        The gateway record, or an empty mapping when the document does not carry one.
        Empty is not an error here -- :func:`_egress_problem` is what names it.
    """

    if not isinstance(document, dict):
        return {}
    record = document.get("egress")
    return record if isinstance(record, dict) else {}


def _egress_problem(raw: str) -> str | None:
    """Say why this ``egress.json`` would leave the reviewer's traffic unproxied.

    🔴 **Kitty disables egress SILENTLY for most malformed stores, and its fail-closed
    guard does not cover it.** ``egress_block_reason`` opens with
    ``if egress is None: return None`` (``src/kitty/egress_guard.py:49-51``), so a store
    that resolves to nothing makes the guard pass by having nothing to guard: ``kitty
    claude`` launches, the whole review runs from the runner's own IP, and the check goes
    **green**. That is what upstream exists to close, and why every shape below is a
    refusal rather than a warning.

    Measured against ``kitty-bridge`` 1.5.0 -- the probe that produced this list is
    committed at
    ``.requirements/20260829T110316Z_kitty_egress_actually_used/probe_egress_shapes.py``:

    * a document with **no ``version``** (a bare record, the usual shape of a hand-pasted
      export) and one whose ``version`` kitty does not know both return ``None`` from
      ``load()`` with only a ``logger.warning`` nothing here reads;
    * ``{"version": 1, "egress": null}`` -- what ``kitty egress`` → *Remove gateway*
      writes -- returns ``None`` with **no log line at all**;
    * ⚠️ an **empty ``proxy_url``** is the one that does not look like a failure. It
      loads: ``from_dict`` raises only on a *missing* key and ``EgressConfig``
      validates only the username/password pair, so kitty reports a healthy gateway and
      exits 0, while ``aiohttp_session_kwargs`` hands aiohttp ``proxy=""``, which aiohttp
      ignores -- measured, a request then went straight to the destination. **This is the
      only shape the runtime ``kitty egress show`` gate cannot see**, which is why that
      gate does not make this function redundant.

    ⚠️ **``version`` is required to be NUMERIC, deliberately NOT required to equal 1.**
    Pinning it would turn a ``STORE_VERSION`` bump -- reached by the unpinned
    ``pip install --upgrade kitty-bridge`` this workflow runs every time -- into a hard
    ``fatal`` on every pull request, because this check would refuse a config kitty
    accepts. Version compatibility is the runtime gate's business: that is kitty
    answering about itself, and it cannot go stale.

    ⚠️ **Everything else stays as lenient as kitty**, per the rule in
    :func:`_unresolved_references`: inventing a second opinion turns a config kitty
    accepts into a refusal. The empty ``proxy_url`` rule is the **single** deliberate
    exception, because that shape is measurably unproxied.

    🔴 **That leniency rule is load-bearing, and the first draft of this function broke
    it while claiming to honour it.** It refused a boolean or float ``version`` as "not
    an integer" -- and kitty accepts both, because its test is ``!= STORE_VERSION`` and
    Python's ``True == 1 == 1.0``. Measured against 1.5.0: all three resolve the gateway.
    The refusal message even asserted the opposite ("kitty reads it as no gateway at
    all"). Nothing caught it, because the committed probe covered ``1``, ``2`` and
    ``None`` and not the two shapes the code had an opinion about -- **a check written
    against shapes nobody measured, which is precisely the defect this whole module was
    changed to fix.** The probe now covers them.

    Args:
        raw: The setting's value, already verified to be valid JSON by
            :func:`_read_and_validate`, which is the only caller.

    Returns:
        One description naming the field at fault, or ``None`` when the document would
        enable a gateway. Never quotes a value -- field names and the setting name only.

    Raises:
        json.JSONDecodeError: If ``raw`` is not valid JSON, i.e. if the caller's
            precondition is broken. Deliberately not caught: a malformed value is
            already reported by name upstream, and swallowing it here would turn a
            caller bug into a silently skipped egress check -- the failure mode this
            whole function exists to remove.
    """

    document = json.loads(raw)
    if not isinstance(document, dict):
        return "KITTY_EGRESS_JSON (egress.json) is not a JSON object"

    # NUMERIC, not "an integer, and not a bool". 🔴 A first version of this rule excluded
    # `bool` -- on the reasoning that `isinstance(True, int)` is True in Python and
    # `{"version": true}` is a paste artefact -- and it was WRONG in the one direction
    # this function must never be wrong in. Kitty's own test is
    # `data.get("version") != STORE_VERSION` (`egress_store.py:141`), which is plain
    # equality: `True == 1` and `1.0 == 1`, so kitty ACCEPTS both and proxies. Measured
    # against 1.5.0, all three of `true`, `1.0` and `1` resolve the gateway and exit 0.
    # Refusing them would be exactly the self-inflicted outage this rule's own leniency
    # argument exists to prevent -- this script failing a build over a configuration
    # kitty is perfectly happy with.
    #
    # What the rule still buys: a document carrying `egress` but no `version` at all,
    # and one whose version is a STRING (`"1" != 1`), are both refused by kitty, and
    # naming them here beats waiting for the runtime gate to say only "no gateway".
    # The VALUE is deliberately not pinned -- see this function's docstring.
    version = document.get("version")
    if not isinstance(version, (int, float)):
        return (
            "KITTY_EGRESS_JSON (egress.json) has no numeric 'version' field, so kitty "
            "reads it as no gateway at all and the review would run unproxied"
        )

    if not isinstance(document.get("egress"), dict):
        return (
            "KITTY_EGRESS_JSON (egress.json) has no 'egress' object -- an absent or "
            "null gateway is how kitty records one that was REMOVED, and the review "
            "would run unproxied"
        )

    proxy_url = _egress_record(document).get("proxy_url")
    if not isinstance(proxy_url, str) or not proxy_url.strip():
        return (
            "KITTY_EGRESS_JSON (egress.json) has no non-empty 'proxy_url' -- kitty "
            "accepts this and reports a healthy gateway, but sends every request "
            "directly"
        )

    return None


def _unresolved_references(values: dict[str, str]) -> list[str]:
    """Name every credential reference the config needs and ``credentials.json`` lacks.

    🔴 **The three settings are one config with references across them, and replacing any
    one of them alone can break the other two silently.** ``profiles.json`` points at a
    credential by ``auth_ref``; ``egress.json`` points at the proxy password the same way.
    ``credentials.json`` is a flat ``{ref: base64_key}`` map, so a reference with no entry
    is simply absent -- there is no error until kitty tries to resolve it, at launch.

    ⚠️ **Measured on PR #546, and the reason this is a check rather than a comment.** An
    operator replaced ``credentials.json`` wholesale from a working local file. It was valid
    JSON, resolved all six provider profiles, and was missing exactly one entry: the egress
    gateway's password. Kitty's egress check is **fail-closed and runs before the model
    call**, so it refused to launch, wrote nothing to its debug log, and handed the action an
    empty execution record. Both attempts were then classified as ``exhausted`` and the
    pull request was told *"The model provider was unavailable ... the provider quota needs
    topping up"* -- a confident, wrong diagnosis pointing at a healthy provider, posted to
    ten pull requests at once. The failure is a **missing map key**, and it is knowable here,
    before anything launches.

    ⚠️ **References are UUIDs, and a UUID is not a secret** -- ``profiles.json`` is a
    repository *variable* whose full text, ``auth_ref``s included, is already printed in
    every run log. What must never appear is a credential *value*, and this function reads
    only the keys of that map, never a value.

    🔴 **The field name ``auth_ref`` is kitty's -- but this docstring cited the wrong
    function for two releases, and that is how a read at the wrong nesting level came to
    look verified (upstream).** It said ``load()`` reads ``data.get("auth_ref")``. It does
    not: ``load()`` reads ``data.get("egress")`` off the DOCUMENT
    (``kitty-bridge`` 1.5.0, ``src/kitty/egress_store.py:145-149``) and it is
    ``EgressRecord.from_dict`` that reads ``auth_ref`` off the nested RECORD (``:78-82``).
    A stored file is ``{"version": 1, "egress": {"proxy_url": …, "username": …,
    "auth_ref": …}}`` (``save()``, ``:161``). Reading ``auth_ref`` at the top level
    therefore matched nothing a real kitty ever writes, and the PR #546 check this
    function exists to be was a **silent no-op from the day it merged** -- proven by
    correcting the test fixtures, which turned exactly one test red.

    ⚠️ **A kitty citation here names a function AND a version; re-read both when the
    version moves.** ``credentials.json`` is still a flat ``{ref: key}`` map
    (``src/kitty/credentials/file_backend.py``, ``get(ref)`` is ``data.get(ref)``), and
    every runtime path builds ``CredentialStore(backends=[FileBackend()])`` with no
    keyring in the chain -- so a keyring-held password is not an alternative explanation
    for a missing entry: the value must be in the file.

    Args:
        values: File names mapped to their verified JSON strings.

    Returns:
        One description per unresolved reference, naming where it is required and which id
        is missing. Empty when every reference resolves, and when the shapes are ones this
        check cannot read -- kitty reports those itself, and inventing a second opinion here
        would turn a config kitty accepts into a refusal.
    """
    try:
        credentials = json.loads(values.get("credentials.json", "{}"))
    except json.JSONDecodeError:  # pragma: no cover - validated before this runs
        return []
    if not isinstance(credentials, dict):
        return ["credentials.json is not an object of {reference: key}"]

    required: list[tuple[str, str]] = []

    # profiles.json: every profile that names a credential. A balancing profile names
    # members rather than an auth_ref, so it contributes none of its own.
    try:
        profiles = json.loads(values.get("profiles.json", "{}"))
        entries = profiles.get("profiles", []) if isinstance(profiles, dict) else []
        if isinstance(entries, dict):
            entries = list(entries.values())
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("auth_ref"), str):
                name = entry.get("name")
                label = f"profiles.json profile {name!r}" if name else "profiles.json"
                required.append((label, entry["auth_ref"]))
    except (json.JSONDecodeError, AttributeError):  # pragma: no cover - defensive
        pass

    # egress.json: the proxy password, when the gateway needs one. The reference
    # lives inside the stored RECORD, not at the top of the document -- see
    # `_egress_record` for the envelope and for what reading the wrong level cost.
    try:
        record = _egress_record(json.loads(values.get("egress.json", "{}")))
        # Truthiness, matching kitty's own `if record.auth_ref:` guard
        # (`egress_store.py:231`): an EMPTY reference means "unauthenticated proxy",
        # not "a reference that resolves to nothing". Measured -- kitty resolves such
        # a gateway and exits 0. An `isinstance(..., str)` test here instead reported
        # `needs credential , which credentials.json does not contain`, naming nothing
        # and refusing a config kitty proxies.
        if record.get("auth_ref"):
            required.append(("egress.json (the proxy password)", record["auth_ref"]))
    except json.JSONDecodeError:  # pragma: no cover - validated before this runs
        pass

    return [
        f"{where} needs credential {ref}, which credentials.json does not contain"
        for where, ref in required
        if not credentials.get(ref)
    ]


def _decode_failure(raw: str, error: json.JSONDecodeError) -> str:
    """Describe WHERE a setting stopped being JSON, without quoting any of it.

    🔴 **This exists because "not valid JSON" cost a CI round-trip and still did not
    say what was wrong.** Measured on PR #546: an operator re-set
    ``KITTY_CREDENTIALS_JSON`` from a file that was itself valid JSON, 2340 bytes and
    BOM-free, and the run reported only the variable's name -- so the corruption was
    known to be somewhere in the transfer and nowhere more precisely than that. Every
    candidate has a distinct signature and none of them needs the value:

    * a **UTF-8 BOM** fails at ``pos 0`` with kitty's own decoder hint. ``.strip()``
      does not remove one, because ``\\ufeff`` is not whitespace -- so a file that looks
      identical in every editor fails at character zero;
    * **smart quotes**, from a paste through a rich-text surface, fail at ``pos 1``;
    * a **truncated paste** -- the usual shape being a multi-line body typed into an
      interactive ``gh secret set``, which keeps only the first line -- fails at or near
      the end, and its length disagrees with the source file's;
    * a value wrapped in **shell quotes** fails at ``pos 0`` with a different message.

    ⚠️ **Two of the decoder's messages DO embed one input character, and this truncates
    them rather than trusting a docstring not to.** The claim here was originally the flat
    "``JSONDecodeError`` never carries content"; the automated review found it false and
    measuring settled it. On the C accelerator -- CPython's default, and what CI runs --
    the catalogue is fixed. On the **pure-Python** scanner, reachable on PyPy or a CPython
    built without ``_json``, two messages interpolate a single character::

        C accelerator   'Invalid control character at'      'Invalid \\escape'
        pure Python     "Invalid control character '\\x01' at"  "Invalid \\escape: 'q'"

    One character of a base64 key is a small leak and still a leak, and "usually base64,
    so it will not fire" is a probability, not a guarantee. Those two messages are
    therefore cut to their fixed prefix. Every other message is emitted whole -- including
    ``Expecting ',' delimiter``, whose quotes are part of the constant, not of the input.

    Args:
        raw: The setting's value, already stripped of edge whitespace.
        error: The decoder's own failure.

    Returns:
        A one-line description naming the failure, its position and the value's length.
    """
    message = error.msg
    for interpolating in ("Invalid control character", "Invalid \\escape"):
        if message.startswith(interpolating):
            message = interpolating
            break
    return (
        f"{message} at line {error.lineno} column {error.colno} "
        f"(offset {error.pos} of {len(raw)} characters)"
    )


class _ConfigurationError(Exception):
    """A named configuration problem this script can report safely."""


def _append(path: str, lines: list[str]) -> None:
    """Append lines to a GitHub Actions output file.

    Args:
        path: Target file, normally ``$GITHUB_OUTPUT``.
        lines: Lines to append, without trailing newlines.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _fail(output_path: str | None, reason: str) -> None:
    """Report an unusable configuration without exiting non-zero.

    Args:
        output_path: ``$GITHUB_OUTPUT``, or ``None`` to report to stdout only.
        reason: Names a setting or an exception class -- never a value.
    """

    if output_path:
        _append(output_path, ["available=false", f"reason={reason}"])
    print(f"::error::Kitty is not available: {reason}")


def _join(names: list[str]) -> str:
    """Join setting names into a report phrase.

    Args:
        names: One or more setting descriptions.

    Returns:
        The names joined for a report message, e.g. ``"A and B"``.
    """

    return " and ".join(names)


def _read_and_validate() -> dict[str, str]:
    """Read the three settings and validate them before anything is written.

    Returns:
        File names mapped to their verified JSON strings.

    Raises:
        _ConfigurationError: Naming every setting that is missing or not
            valid JSON. Both classes are collected across all three settings
            before the error is raised, so one report names every problem at
            once.
    """

    # One pass over all three, collecting both failure classes rather than
    # raising on the first. An operator sets these three together, so a report
    # that names only the unset one costs them a second CI round-trip to be
    # told about the malformed one. An unset variable is named by its binding
    # name; a present-but-unparseable one is named by its variable AND its
    # file, because the file is what they would fix in their local kitty setup.
    values: dict[str, str] = {}
    missing = []
    invalid = []
    for env_name, file_name in KITTY_CONFIG_INPUTS:
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            missing.append(env_name)
            continue
        try:
            json.loads(raw)
        except json.JSONDecodeError as error:
            invalid.append(f"{env_name} ({file_name}): {_decode_failure(raw, error)}")
        else:
            values[file_name] = raw

    # Reported in a fixed order -- missing before malformed -- so the message is
    # deterministic regardless of which setting failed which way.
    problems = []
    if missing:
        problems.append(_join(missing) + " not set")
    if invalid:
        problems.append(_join(invalid) + " not valid JSON")

    # Shape and cross-file references, but only once all three parse -- a check over a
    # value that is not JSON would report a second, derived problem for one cause.
    #
    # ⚠️ The egress SHAPE is reported first and does NOT suppress the reference check;
    # a document can genuinely be both malformed and dangling, and both lines are then
    # printed. That is deliberate for the reason the collection above is: an operator
    # sets these three settings together, and a report naming one fault costs them a
    # CI round-trip to be told about the other. Suppressing is also not available --
    # `_unresolved_references` covers `profiles.json` too, so short-circuiting on a bad
    # egress shape would hide an unrelated PROFILE reference fault.
    if not problems:
        egress_problem = _egress_problem(values["egress.json"])
        if egress_problem:
            problems.append(egress_problem)
        problems.extend(_unresolved_references(values))

    if problems:
        raise _ConfigurationError("; ".join(problems))

    return values


def _write_config(config_dir: Path, values: dict[str, str]) -> None:
    """Write the three kitty config files with restrictive permissions.

    Edge whitespace the operator's paste introduced was already trimmed; the
    JSON itself is written verbatim -- no re-serialization, no key reordering.

    Args:
        config_dir: Kitty's config directory, created if absent.
        values: File names mapped to verified JSON strings.
    """

    # kitty re-tightens credentials.json on its own writes but leaves the
    # other two to the umask, so all three are chmod-ed here explicitly.
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    for file_name, content in values.items():
        target = config_dir / file_name
        target.write_text(content + "\n", encoding="utf-8")
        os.chmod(target, 0o600)


def _write_wrapper(wrapper_dir: Path) -> Path:
    """Write the launcher, alone in a dedicated directory.

    The action puts this directory on PATH, so nothing else may live in it:
    a neighbour of the wrapper could shadow a tool a later step needs.

    Args:
        wrapper_dir: Dedicated launcher directory, created if absent.

    Returns:
        The launcher's absolute path.

    Raises:
        _ConfigurationError: When ``$pythonLocation`` is unset, so there is no
            provisioned interpreter to address kitty beside.
    """

    # upstream. Named here rather than falling back to a bare `kitty`: a fallback would
    # reinstate the PATH lookup silently, on the one launch path the whole review job
    # runs through. The workflow's `Set up Python` step is unconditional and precedes
    # this one, so unset means a real misconfiguration -- and it is the same
    # misconfiguration that would make the *next* step's `pip install` fail anyway,
    # only there it fails without naming a setting.
    location = os.environ.get(PYTHON_LOCATION, "")
    if not location:
        raise _ConfigurationError(f"{PYTHON_LOCATION} not set")

    wrapper_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(wrapper_dir, 0o700)
    wrapper = wrapper_dir / WRAPPER_BASENAME
    wrapper.write_text(
        wrapper_body(f"{location}/bin/kitty"),
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)
    return wrapper.resolve()


def _default_wrapper_dir() -> Path:
    """Locate the launcher directory the workflow expects.

    Returns:
        ``$RUNNER_TEMP/kitty-bridge-bin``, falling back to the platform
        temporary directory for manual local runs.
    """

    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / "kitty-bridge-bin"


def main() -> int:
    """Materialise the kitty configuration and report the launcher path.

    Returns:
        Always 0. Every configuration problem -- missing settings, invalid
        JSON, an unexpected exception -- is reported as ``available=false``
        with a ``::error::`` annotation; the workflow, not this module,
        decides that the run is ``fatal``. ``KeyboardInterrupt`` and
        ``SystemExit`` are not configuration failures and propagate.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        default=str(Path.home() / ".config" / "kitty"),
        help="Directory for profiles.json, credentials.json and egress.json.",
    )
    parser.add_argument(
        "--wrapper-dir",
        default=None,
        help="Dedicated directory for the launcher "
        f"({WRAPPER_BASENAME}); defaults to $RUNNER_TEMP/kitty-bridge-bin.",
    )
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    wrapper_dir = Path(args.wrapper_dir) if args.wrapper_dir else _default_wrapper_dir()

    try:
        values = _read_and_validate()
        _write_config(Path(args.config_dir), values)
        wrapper = _write_wrapper(wrapper_dir)
        if args.github_output:
            _append(args.github_output, ["available=true", f"wrapper_path={wrapper}"])
        print(f"Kitty configured under {args.config_dir}; launcher at {wrapper}")
        return 0
    except Exception as exc:
        # Deliberately broad: this handler is what keeps every configuration
        # failure classified (module docstring). The reason carries only
        # setting names (ours) or the exception class -- never a value.
        if isinstance(exc, _ConfigurationError):
            reason = str(exc)
        else:
            reason = type(exc).__name__
        try:
            _fail(args.github_output, reason)
        except Exception:
            # Reporting must never turn a configuration failure into an
            # unclassified crash; if even the report cannot be written, the
            # absent ``available`` output already gates the review step away.
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
