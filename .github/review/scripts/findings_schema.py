"""Project the findings schema for the CLI, and publish the caps it declares.

``--json-schema`` in Claude Code is **not** the Claude API's structured-outputs
feature, and the schema in this repository was written as though it were.

* The API constrains decoding server-side. Its own documentation says "no
  retries needed for schema violations", and constraint keywords such as
  ``maxLength`` are **not supported** there — the first-party SDKs strip them,
  restate them in the description, and validate the response themselves.
* Claude Code exposes a ``StructuredOutput`` **tool**, compiles the schema into
  an **ajv** validator, and re-prompts the model on any mismatch. ajv enforces
  every constraint keyword. Validation is all-or-nothing over the whole
  document, so one over-long ``title`` invalidates the summary and every
  finding — and when the attempts run out the run ends
  ``error_max_structured_output_retries`` with **no review at all**, billed in
  full. Measured on PR #401: 224s of model time and $4.73 for no output.

This module is the fix, and it deliberately copies the first-party SDKs' shape
rather than inventing one:

1. :func:`strip_for_cli` removes every constraint keyword from what reaches the
   CLI, so a cap can no longer cost a whole review, and
2. **restates each one in that property's ``description``**, so the model is
   still told the cap. Stripping without this step would leave it guessing, and
   an unguided model writes 8,000-character summaries.
3. :func:`caps` hands the same numbers to ``post_review.py``, which enforces
   them **after** the review comes back — where over-running one costs a
   truncated string rather than a lost review.

The schema **file** keeps every constraint and stays the contract. It is the
single place a cap is written down: this module reads it, and nothing else
restates it. A number retyped into a second file is a number that drifts.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import cast

#: Keywords ajv enforces that the model cannot reliably satisfy first time, and
#: whose failure costs the entire document rather than the offending field.
#: Removed from what reaches ``--json-schema``; still honoured on the way out.
#:
#: ``uniqueItems`` and ``multipleOf`` are not used by the schema today. They are
#: listed because the cost of a keyword nobody added yet is one set membership,
#: while the cost of missing one is a class of lost review that took two runs
#: and $9 to diagnose. Anything shape-bearing — ``type``, ``enum``, ``const``,
#: ``required``, ``additionalProperties`` — is deliberately absent: those are
#: what make the payload parseable, and the CLI must keep enforcing them.
CONSTRAINT_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "multipleOf",
        "uniqueItems",
        "pattern",
    }
)

#: How each stripped keyword is restated in prose. The wording names the
#: keyword's *meaning*, not its JSON name, because the audience is a model
#: reading a description rather than a validator reading a schema -- these
#: descriptions ARE prompt text, which is why the grammar is worth getting
#: right. ``{value}`` and ``{noun}`` are substituted; ``{noun}`` is already
#: pluralised for the value.
_PHRASING = {
    "maxLength": "At most {value} {noun}.",
    "minLength": "At least {value} {noun}.",
    "maxItems": "At most {value} {noun}.",
    "minItems": "At least {value} {noun}.",
    "minimum": "The smallest allowed value is {value}.",
    "maximum": "The largest allowed value is {value}.",
    "exclusiveMinimum": "Must be greater than {value}.",
    "exclusiveMaximum": "Must be less than {value}.",
    "multipleOf": "Must be a multiple of {value}.",
    "uniqueItems": "Every item must be distinct.",
    "pattern": "Must match the regular expression {value}.",
}

#: Keywords whose lower bound of 1 is better said as "not empty". "At least 1
#: characters" is both ungrammatical and uninformative, and it would be read by
#: the model on every run.
_NOT_EMPTY = {"minLength": "Must not be empty.", "minItems": "Must not be empty."}

#: The unit each length keyword counts.
_NOUN = {
    "maxLength": "character",
    "minLength": "character",
    "maxItems": "item",
    "minItems": "item",
}

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "review_findings.schema.json"
)


def load(path: str | Path | None = None) -> dict:
    """Read the findings schema from disk.

    Args:
        path: Schema file, or None for the repository's own.

    Returns:
        The decoded schema.

    Raises:
        OSError: The file cannot be read.
        json.JSONDecodeError: The file is not valid JSON.
    """

    target = Path(path) if path else DEFAULT_SCHEMA_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def _describe(node: dict, removed: list[tuple[str, object]]) -> None:
    """Fold removed constraints into a node's description.

    Args:
        node: The schema node the constraints were removed from.
        removed: ``(keyword, value)`` pairs, in schema order.
    """

    if not removed:
        return

    sentences = []
    for keyword, value in removed:
        # `uniqueItems: false` states no constraint at all, so restating it
        # would tell the model something untrue.
        if keyword == "uniqueItems" and not value:
            continue
        if keyword in _NOT_EMPTY and value == 1:
            sentences.append(_NOT_EMPTY[keyword])
            continue
        template = _PHRASING.get(keyword)
        if template is None:
            continue
        noun = _NOUN.get(keyword, "")
        if noun and value != 1:
            noun += "s"
        sentences.append(template.format(value=value, noun=noun))

    if not sentences:
        return

    existing = str(node.get("description", "")).strip()
    node["description"] = " ".join([existing, *sentences]).strip()


def strip_for_cli(schema: dict) -> dict:
    """Return the schema with constraint keywords moved into descriptions.

    Walks the whole document, so a constraint nested inside ``items`` or a
    sub-object is caught as surely as a top-level one. The input is not
    modified.

    Args:
        schema: The schema as declared in the file.

    Returns:
        A copy carrying shape only, with every removed constraint restated in
        the enclosing property's description.
    """

    # 🔴 Maps whose keys are PROPERTY NAMES, not schema keywords. Inside one of
    # these, a key called `pattern` is a field the review is expected to return,
    # not a constraint -- and stripping it would delete the property while
    # leaving it in `required` and forbidden by `additionalProperties: false`.
    # That is an unsatisfiable schema: exactly the failure class this module
    # exists to remove, reintroduced by the removal. Not live today; `pattern`
    # is a plausible future field given `other_instances` already asks for "the
    # search that would find them".
    property_maps = ("properties", "$defs", "definitions", "patternProperties")

    def walk(node: object, in_property_map: bool = False) -> object:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node

        out: dict = {}
        removed: list[tuple[str, object]] = []
        for key, value in node.items():
            if key in CONSTRAINT_KEYWORDS and not in_property_map:
                removed.append((key, value))
                continue
            # 🔴 `not in_property_map and ...` -- the mirror case, and the fix
            # for it reintroduced the very defect this module removes. INSIDE a
            # property map the keys are NAMES, so a property named `definitions`
            # would otherwise set the flag for its OWN schema and its `maxLength`
            # would reach the ajv validator untouched. Measured: it did.
            out[key] = walk(value, not in_property_map and key in property_maps)

        # Description folding happens after the walk so the sentences land after
        # whatever description the node already had, in schema order.
        _describe(out, removed)
        return out

    # ``walk`` is shape-preserving -- a dict node yields a dict -- and ``schema``
    # is a dict, so this is a dict. Cast rather than an isinstance check: the
    # invariant is in the recursion, which a checker cannot follow, and a runtime
    # assertion here would be testing this module against itself.
    return cast(dict, walk(copy.deepcopy(schema)))


def _cap(node: object, keyword: str) -> int | None:
    """Read one integer constraint off a schema node.

    Args:
        node: Candidate schema node.
        keyword: Constraint keyword to read.

    Returns:
        The declared value, or None when absent or not a plain integer. A
        missing cap means "do not enforce", never "enforce zero".
    """

    if not isinstance(node, dict):
        return None
    value = node.get(keyword)
    # `bool` is an `int` in Python, and a `True` here would silently become a
    # one-character cap that truncates every string in the review.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def caps(schema: dict) -> dict:
    """Extract the caps ``post_review.py`` enforces.

    Derived from the schema rather than restated, so editing the file changes
    what is enforced and the two can never disagree.

    Args:
        schema: The schema as declared in the file.

    ⚠️ **The document-level field NAMES are listed, not derived, and the
    numbers are not.** Every cap comes from the file; only the set of top-level
    fields eligible for truncation is written down here. Deriving it would be
    four lines and would produce exactly this set today — it does **not** sweep
    up ``findings``, which declares ``maxItems`` rather than ``maxLength``. The
    reason to list them is what deriving would do *later*: a future top-level
    string field carrying a ``maxLength`` would start being silently truncated
    without anyone deciding it should be. Deferred rather than change
    enforcement behaviour by accident; adding a field here is the deliberate
    act that turns its cap on.

    Returns:
        A mapping with ``document`` (top-level string caps by field name),
        ``findings_max``, ``finding_text`` (per-finding string caps by field
        name), ``other_instances_max`` and ``other_instance_max``.
    """

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}

    findings = properties.get("findings", {})
    items = findings.get("items", {}) if isinstance(findings, dict) else {}
    finding_properties = items.get("properties", {}) if isinstance(items, dict) else {}
    if not isinstance(finding_properties, dict):
        finding_properties = {}

    other = finding_properties.get("other_instances", {})
    other_items = other.get("items", {}) if isinstance(other, dict) else {}

    return {
        "document": {
            name: _cap(properties.get(name), "maxLength")
            for name in ("summary", "conversation_notes")
        },
        "findings_max": _cap(findings, "maxItems"),
        "finding_text": {
            name: _cap(node, "maxLength")
            for name, node in finding_properties.items()
            if _cap(node, "maxLength") is not None
        },
        "other_instances_max": _cap(other, "maxItems"),
        "other_instance_max": _cap(other_items, "maxLength"),
    }


def compact_for_cli(schema: dict) -> str:
    """Render the CLI-facing schema as a single-quote-safe compact string.

    Args:
        schema: The schema as declared in the file.

    Returns:
        Compact JSON, ready to be interpolated into a single-quoted shell
        argument.

    Raises:
        ValueError: The result contains a single quote, which would close the
            shell argument early and hand the CLI truncated JSON.
    """

    projected = strip_for_cli(schema)

    # The CLI validates the schema with an offline validator that cannot resolve
    # a remote meta-schema and rejects the whole argument with "no schema with
    # key or ref ...". The key is optional metadata, so it goes here while the
    # source file keeps it for editor tooling.
    projected.pop("$schema", None)

    compact = json.dumps(projected, separators=(",", ":"))

    if "'" in compact:
        excerpts = [
            compact[max(0, i - 60) : i + 20]
            for i, char in enumerate(compact)
            if char == "'"
        ]
        raise ValueError(
            f"the schema contains {len(excerpts)} single quote(s). They break shell "
            "quoting in claude_args. Rewrite the affected descriptions without "
            "apostrophes:\n" + "\n".join(f"  ...{e}..." for e in excerpts[:10])
        )
    return compact


def main() -> int:
    """Print the schema that should reach ``--json-schema``.

    Returns:
        0 on success, 1 when the schema cannot be read or is not shell-safe.
        A non-zero exit here is deliberate: the alternative is handing the CLI a
        truncated argument, which fails one step later with a message naming
        neither this file nor the offending text.
    """

    parser = argparse.ArgumentParser(
        description="Project the findings schema for the CLI."
    )
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH))
    args = parser.parse_args()

    try:
        schema = load(args.schema)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"cannot read {args.schema}: {exc}\n")
        return 1

    try:
        compact = compact_for_cli(schema)
    except ValueError as exc:
        sys.stderr.write(f"{args.schema}: {exc}\n")
        return 1

    stripped = sorted(
        {
            keyword
            for keyword in CONSTRAINT_KEYWORDS
            if f'"{keyword}":' in json.dumps(schema, separators=(",", ":"))
        }
    )
    sys.stderr.write(
        "note: stripped $schema and the constraint keywords "
        f"({', '.join(stripped) or 'none present'}); each is restated in its "
        "description. The UPPER bounds are re-enforced in post_review.py and "
        "conversation_notes in interpret_claude_result.py; the rest are "
        "enforced nowhere -- see SYSTEM_DESIGN.md section 14.3\n"
    )
    print(compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
