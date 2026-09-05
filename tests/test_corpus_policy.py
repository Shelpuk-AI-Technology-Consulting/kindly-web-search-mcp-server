"""Policy guard for the saved-HTML corpus under ``tests/corpus/html/``.

Section 3.3 of ``.system_design/TEST_SUITE.md`` governs what may be committed
there: two tiers, a provenance sidecar per file, sanitization before commit, and
a size cap per tier. This module is what enforces it.

**This repository is public, so a committed corpus file is a published
document.** That is the whole reason the guard exists. The failure it prevents is
not a broken test -- it is a contributor saving a page that still carries a
session cookie, an ``Authorization`` header, a bearer-shaped token, a reader's
email address or a third-party analytics script body, and that page being served
from a public URL for as long as git history exists.

Three groups of cases, in the order they appear:

**Document agreement.** :data:`POLICY` and section 3.3's fenced JSON block must
be equal, compared in both directions, so editing either alone turns the suite
red. This is the house pattern set by ``test_dependency_constraints.py``: a
document that describes a policy nothing checks is a document that drifts.
Because both copies are edited together by design, the sets the plan step's
verify clause names are pinned a **third** time, against literals below -- see
:data:`MANDATED_SANITATION_ROWS` and
:func:`test_the_provenance_field_sets_are_pinned_against_literals`. Without that
third anchor, deleting ``capture_date`` from the document and the constant in one
pull request leaves every parametrized sweep smaller and the suite green.

**The committed tree.** :func:`check_corpus` is run once over the real corpus and
each case filters the result to a single rule, so N failing cases mean N
problems rather than one case reporting a pile.

**That the policy fires.** Every rule is driven against a synthetic corpus in
``tmp_path`` that breaks exactly that rule and nothing else, and each such case
asserts the **exact** violation list rather than merely a non-empty one. A rule
proved only by "some violation was reported" is a rule that can be satisfied by
the wrong one.

The synthetic cases start from a *conforming* corpus and apply one defect. That
is what makes "exactly one violation, of this rule" a claim worth making, and it
is why :func:`test_a_conforming_synthetic_corpus_has_no_violations` is not
decoration: an implementation that reported one spurious violation on every file
would otherwise be invisible in both directions.

**Two committed-tree claims are vacuous today, and a reader should know which
before trusting a green run.** No snapshot *page* is committed, so no committed
sidecar exercises ``required_any_of`` -- fragments declare that list empty. The
second is stronger than "unexercised": ``field_patterns`` covers exactly
``source_url`` and ``capture_date``, both of which the fragments tier *forbids*,
and the field check skips a forbidden key, so with no snapshot sidecar committed
:func:`test_every_committed_provenance_field_is_well_formed` is **structurally**
unreachable rather than merely untriggered. Both are carried by the synthetic
cases alone until the first real snapshot lands.

**The tier directory is not empty, and the file-level rules do run there.**
``snapshots/README.md`` is committed, so the size cap -- 200 KB, for that tier --
along with the sanitation sweep, the CRLF and decode rules and the tier and
extension checks are all measured against committed bytes in ``snapshots/``
today. An earlier draft of this paragraph said the tier "ships empty, so no
committed file is ever measured against the 200 KB cap", which was false from
the first commit: what ships empty is the set of snapshot *pages*, and only the
rules that need a page or its sidecar are waiting for one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, TypeGuard

import pytest

# `_section_body` is imported rather than copied: the fence-aware section bound
# is one helper with several users already, and another copy would be another
# thing to fix. Section 3.3 holds a fenced block whose contents include lines
# that a naive heading regex would read as headings.
from tests.test_pytest_configuration import _section_body

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The design document this guard is the executable half of.
TEST_SUITE_PATH = REPO_ROOT / ".system_design" / "TEST_SUITE.md"

#: The exact heading the policy block lives under. Asserted to exist before the
#: section is parsed, so a rename produces a message naming the heading rather
#: than an index error from the section walker.
DESIGN_SECTION_HEADING = "### 3.3 HTML corpus governance"

#: The corpus root. Every path in a :class:`Violation` is relative to it.
CORPUS_ROOT = REPO_ROOT / "tests" / "corpus" / "html"

#: The directory `tests/corpus/`, one level above the root. Named rather than
#: spelled `CORPUS_ROOT.parent`, so the collection cases read as the claim they
#: make -- nothing anywhere under `tests/corpus/` is importable or collectable.
CORPUS_TREE = CORPUS_ROOT.parent

#: The policy, held as a module constant and compared against the design
#: document in both directions. Keeping a second copy here rather than simply
#: parsing the document is deliberate and is this repository's house pattern: a
#: guard whose only source of truth is the document it checks cannot notice a
#: change to that document, and the point of the check is that a policy edit is
#: a reviewed, two-file edit.
POLICY: dict[str, Any] = {
    "sidecar_suffix": ".meta.json",
    "allowed_extensions": [".html", ".meta.json"],
    "allowed_filenames": ["README.md"],
    "required_fragments": [
        "code_block",
        "html_entities",
        "nested_lists",
        "table_basic",
    ],
    "min_fragment_text_chars": 250,
    "tiers": {
        "fragments": {
            "max_bytes": 8192,
            "required": ["rationale"],
            "required_any_of": [],
            "forbidden": ["source_url", "capture_date"],
        },
        "snapshots": {
            "max_bytes": 204800,
            "required": ["source_url", "capture_date"],
            "required_any_of": ["licence", "rationale"],
            "forbidden": [],
        },
    },
    "field_patterns": {
        "source_url": r"^https?://\S+$",
        "capture_date": r"^\d{4}-\d{2}-\d{2}$",
    },
    "sanitation_patterns": {
        "set_cookie": {
            # Three shapes, because a saved page is an HTTP *body* and the
            # response header is not in it: the two ways a cookie actually
            # reaches committed HTML are the `http-equiv` meta and a
            # `document.cookie` assignment in inline script, and a row armed
            # only against the header form passes both. The quote class also
            # tolerates a backslash, because the sweep reads a sidecar's bytes
            # and a sidecar is JSON, where an embedded `"` arrives as `\"` --
            # measured: without it the `http-equiv` specimen was caught inside
            # a page and missed inside a sidecar.
            "regex": (
                r"(?i)(?:set-cookie\s*[:=]|document\.cookie\s*=|"
                r"http-equiv\s*=\s*[\"'\\]{0,2}set-cookie)"
            ),
            "matches": [
                "Set-Cookie: sid=EXAMPLE-NOT-REAL; Path=/",
                '<meta http-equiv="Set-Cookie" content="sid=EXAMPLE-NOT-REAL">',
                'document.cookie = "pref=dark; path=/";',
            ],
            "does_not_match": [
                "<p>This site uses cookies to set your preferences.</p>",
            ],
        },
        "session_identifier": {
            # Section 3.3's prose says "session identifiers" and no row covered
            # them. Narrowed to a name followed by a value, which is how one
            # appears in a cookie string or a script assignment: the bare name
            # also appears in documentation prose about sessions, and this
            # scraper's targets include such pages.
            "regex": (
                r"(?i)\b(?:phpsessid|jsessionid|asp\.net_sessionid|sessionid"
                r"|session_id)\b\s*[:=]\s*\S"
            ),
            "matches": [
                "PHPSESSID=EXAMPLE-NOT-REAL",
                "JSESSIONID: EXAMPLE-NOT-REAL",
            ],
            "does_not_match": [
                "<p>The JSESSIONID cookie is set by the container.</p>",
                "<p>Session identifiers are stripped before commit.</p>",
            ],
        },
        "authorization_header": {
            # A scheme keyword *and* a credential-shaped value are both
            # required, because `<h2>Authorization: Bearer tokens</h2>` -- an
            # API documentation heading, the archetypal page this project
            # scrapes and therefore the archetypal snapshot somebody will want
            # to commit -- was measured matching the looser forms. `\S{8,}` is
            # one of them: `tokens</h2>` is eleven non-space characters. A
            # credential character class of twelve is what separates a value
            # from the next word of prose.
            "regex": (
                r"(?i)\bauthorization\s*[:=]\s*[\"'\\]{0,2}"
                r"(?:bearer|basic|digest|token|apikey|api[_-]key)\s+[A-Za-z0-9._~+/=-]{12,}"
            ),
            "matches": [
                "Authorization: Basic RVhBTVBMRQ==NOTREAL",
                'authorization = "Digest username=EXAMPLE0000"',
            ],
            "does_not_match": [
                "<h2>Authorization: Bearer tokens</h2>",
                "<p>Authorization is required before publishing.</p>",
            ],
        },
        "bearer_token": {
            "regex": r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}",
            "matches": ["Bearer RVhBTVBMRS1OT1QtQS1SRUFMLVRPS0VO"],
            "does_not_match": [
                "<p>Bearer of the standard</p>",
                "<p>Please bear with us.</p>",
            ],
        },
        "jwt": {
            "regex": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            "matches": [
                "eyJhbGciOiJIUzI1NiJ9.RVhBTVBMRV9OT1RfUkVBTA.c2lnbmF0dXJlX2V4YW1wbGU"
            ],
            "does_not_match": [
                "<code>eyJ is the base64 prefix of a JSON object.</code>",
            ],
        },
        "email_address": {
            # The leading lookbehind is not cosmetic: without it the `+` retries
            # from every position in a long run of word characters, and the
            # sweep was measured at 58 s on a single 200 KB page. Anchored and
            # possessive it is 0.004 s, with identical results on every
            # specimen. The `@2x` exclusion is what keeps retina asset
            # filenames -- `logo@2x.png`, on a large share of real pages -- from
            # being reported as addresses.
            "regex": (
                r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@"
                r"(?!\d+[xX]\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
            ),
            "matches": [
                "contact reader@example.com for more details",
                '<a href="mailto:info@example.org">write to us</a>',
                "reach us at reader&#64;example.com any time",
            ],
            "does_not_match": [
                "<p>Follow @example on social media.</p>",
                '<img src="logo@2x.png" srcset="logo@2x.png 2x, logo@3x.png 3x">',
            ],
        },
        "google_analytics": {
            # `gtag/js` alone misses the commonest deployment there is: a
            # Tag Manager container loads `gtm.js` and its noscript arm
            # `ns.html`, and carries neither `gtag(` nor `gtag/js`. Measured
            # against Google's current install snippet -- both produced no
            # violation. That is a hole *inside* a row which already claims the
            # host, not one of the vendors section 3.3 leaves to a human.
            "regex": (
                r"(?i)(?:google-analytics\.com/analytics\.js"
                r"|googletagmanager\.com/(?:gtag/js|gtm\.js|ns\.html)"
                r"|\bgtag\s*\(|\b_gaq\.push\s*\()"
            ),
            "matches": [
                (
                    '<script src="https://www.googletagmanager.com/gtag/js'
                    '?id=G-EXAMPLE"></script>'
                ),
                "gtag('config', 'G-EXAMPLE');",
                (
                    '<iframe src="https://www.googletagmanager.com/ns.html'
                    '?id=GTM-EXAMPLE"></iframe>'
                ),
                "_gaq.push(['_setAccount', 'UA-EXAMPLE']);",
            ],
            "does_not_match": ["<p>We removed Google Analytics in 2019.</p>"],
        },
        "segment_analytics": {
            "regex": r"(?i)(?:cdn\.segment\.com/analytics\.js|\banalytics\.load\s*\()",
            "matches": [
                "analytics.load('EXAMPLE_WRITE_KEY');",
                (
                    '<script src="https://cdn.segment.com/analytics.js/v1'
                    '/EXAMPLE/x.js"></script>'
                ),
            ],
            "does_not_match": ["<p>The analytics team load-tests weekly.</p>"],
        },
        "facebook_pixel": {
            "regex": r"(?i)(?:connect\.facebook\.net/\S*fbevents\.js|\bfbq\s*\()",
            "matches": [
                "fbq('init', 'EXAMPLE');",
                (
                    '<script src="https://connect.facebook.net/en_US'
                    '/fbevents.js"></script>'
                ),
            ],
            "does_not_match": ["<p>The fbq abbreviation is not used here.</p>"],
        },
        "hotjar": {
            "regex": r"(?i)(?:static\.hotjar\.com/c/hotjar|\b_hjSettings\b)",
            "matches": [
                "window._hjSettings={hjid:0,hjsv:0};",
                '<script src="https://static.hotjar.com/c/hotjar-0.js"></script>',
            ],
            "does_not_match": ["<p>Hotjar was evaluated and rejected.</p>"],
        },
    },
}

#: The sanitation categories the implementation plan's verify clause names for
#: this step, held as a literal **separate from** :data:`POLICY`.
#:
#: :data:`POLICY` and the design document are edited together by design, so a row
#: deleted from both would shrink the sweep to green with no case noticing. This
#: set is the third edit, and it names the *promise* rather than the
#: implementation: "bearer-shaped tokens" is covered by ``bearer_token`` and
#: ``jwt``, and "known analytics script bodies" by four vendor rows, so only one
#: of each group is named here -- naming all of them would forbid ever replacing
#: one pattern with a better one.
MANDATED_SANITATION_ROWS = frozenset(
    {
        "set_cookie",
        "authorization_header",
        "bearer_token",
        "email_address",
        "google_analytics",
    }
)

#: The provenance obligations the verify clause names, pinned against literals
#: for the same reason as :data:`MANDATED_SANITATION_ROWS`. Every parametrized
#: field sweep below draws its cases from :data:`POLICY`, so it has exactly as
#: many cases as the policy has entries and cannot notice an entry's removal.
MANDATED_TIERS = frozenset({"fragments", "snapshots"})
MANDATED_SNAPSHOT_REQUIRED = ("source_url", "capture_date")
MANDATED_SNAPSHOT_ANY_OF = ("licence", "rationale")
MANDATED_FRAGMENT_FORBIDDEN = ("source_url", "capture_date")
MANDATED_PATTERNED_FIELDS = frozenset({"source_url", "capture_date"})

#: The per-tier byte caps. Section 3.3 calls the fragment cap "the bound that
#: makes the two tiers mechanically distinguishable" and "what stands in for
#: review" on a fresh capture filed as a fragment -- and every boundary case
#: below derives its cap from :data:`POLICY`, so raising the fragment cap to the
#: snapshot cap in both copies moves both sides of every boundary and leaves the
#: suite green. Measured. This literal is what fails instead.
MANDATED_TIER_CAPS = {"fragments": 8192, "snapshots": 204800}

#: The fragment roles section 3.3's prose names -- a table, a code block, nested
#: lists, an entity edge case. Removing a stem from :data:`POLICY` also removes
#: the case that would have noticed, so this is the literal that ties the policy
#: back to the prose. Measured: without it, dropping `html_entities` from both
#: copies and deleting the file left the suite green -- which is exactly the
#: failure :func:`_required_fragment_violations` exists to prevent.
MANDATED_REQUIRED_FRAGMENTS = ("code_block", "html_entities", "nested_lists", "table_basic")


@dataclass(frozen=True, order=True)
class Violation:
    """One policy breach found in one corpus file.

    Attributes:
        rule: The rule that was broken, as a stable identifier. Cases filter on
            this, so it is part of this module's interface rather than message
            text.
        path: The offending file, relative to the corpus root and written with
            forward slashes so a failure message reads the same on every
            platform. For a rule about the corpus as a whole rather than one
            file, the tier directory it concerns.
        detail: What specifically was wrong -- the missing field name, the
            sanitation row id, the measured size. Never the file's contents: a
            failure message is printed to a CI log, and printing the credential
            that must not be published would publish it again.
    """

    rule: str
    path: str
    detail: str


def _design_section(heading: str = DESIGN_SECTION_HEADING) -> str:
    """Return the body of one section of the design document.

    ``heading`` is a parameter, defaulted to the real one, purely so the case
    that proves the assertion below can supply a heading the document does not
    have. The obvious alternative -- ``monkeypatch`` on the module constant --
    would need the constant's *name* as a string literal, and
    ``tests/test_baseline_failure_ledger.py`` scans every literal under
    ``tests/`` for environment-variable shape and cannot tell one from the
    other. Measured: it failed the whole suite. A seam here costs one default
    argument; the alternative costs a row in another guard's exclusion list.

    Args:
        heading: The exact heading line the section starts at.

    Returns:
        Everything under the heading, up to the next heading outside a fenced
        block.

    Raises:
        AssertionError: When the heading is not in the document.
    """
    text = TEST_SUITE_PATH.read_text(encoding="utf-8")
    # Checked before delegating: `_section_body` locates the heading with
    # `list.index`, which raises a bare ValueError naming nothing useful.
    assert heading in text.splitlines(), (
        f"{heading!r} is not a heading in {TEST_SUITE_PATH.name}; this guard "
        "parses that section, and renaming it would silently disable the check."
    )
    return _section_body(text, heading)


def _design_document_policy(section: str | None = None) -> dict[str, Any]:
    """Parse the corpus policy out of section 3.3's fenced JSON block.

    Args:
        section: The section body to parse, defaulted to the real one. A
            parameter for the same reason as :func:`_design_section`'s
            ``heading``: the case that proves the count assertion has to supply
            a section holding two blocks.

    Returns:
        The policy the design document declares.

    Raises:
        AssertionError: When the section does not hold exactly one fenced JSON
            block. An unbounded or multi-block search would start comparing
            against some later block the day one is added, and would report a
            confusing inequality rather than the structural problem.
    """
    body = _design_section() if section is None else section
    blocks = re.findall(r"```json\n(.*?)```", body, re.DOTALL)
    assert len(blocks) == 1, (
        f"section 3.3 of {TEST_SUITE_PATH.name} holds {len(blocks)} fenced json "
        "blocks; this guard requires exactly one."
    )
    return json.loads(blocks[0])


def _extension_of(path: Path) -> str:
    """Return the extension the policy classifies a corpus file by.

    ``Path.suffix`` reports ``.json`` for ``table_basic.meta.json``, which would
    put sidecars and any other JSON file in the same class. The sidecar suffix
    is therefore matched on the whole file name first.

    Args:
        path: The file.

    Returns:
        The sidecar suffix for a sidecar, otherwise the file's own suffix
        (the empty string when it has none).
    """
    suffix = POLICY["sidecar_suffix"]
    return suffix if path.name.endswith(suffix) else path.suffix


def _tier_of(path: Path, root: Path) -> str | None:
    """Return the tier a corpus file belongs to, from its location.

    The tier is derived from the directory rather than read out of the sidecar
    on purpose: a self-declared tier would make the cheapest escape from the
    provenance rules a one-word edit inside the very file those rules govern.

    Args:
        path: The corpus file.
        root: The corpus root.

    Returns:
        The tier name when the file sits directly inside a known tier
        directory, otherwise ``None``.
    """
    relative = path.relative_to(root)
    if len(relative.parts) != 2:
        return None
    tier = relative.parts[0]
    return tier if tier in POLICY["tiers"] else None


def visible_text(html: str) -> str:
    """Return the collapsed visible text of an HTML fragment.

    Deliberately crude -- tags stripped by regular expression, entities
    unescaped, whitespace collapsed -- and deliberately dependency-free. Its one
    job is to measure whether a handcrafted fragment carries enough prose to
    exercise the production extraction path, and importing the extractor to
    answer that would make this guard fail whenever the extractor did.

    Args:
        html: The fragment's markup.

    Returns:
        The fragment's text with script and style bodies removed.
    """
    without_code = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return " ".join(unescape(re.sub(r"(?s)<[^>]*>", " ", without_code)).split())


def corpus_files(root: Path) -> list[Path]:
    """Return every file below the corpus root, sorted.

    Every file, not only the recognised extensions: a sweep that looks only for
    ``.html`` and ``.meta.json`` cannot report the stray ``.png``, ``.js`` or
    editor backup that the extension rule exists to catch.

    Args:
        root: The corpus root.

    Returns:
        Every file below ``root``, in sorted order.
    """
    return sorted(path for path in root.rglob("*") if path.is_file())


def check_corpus(root: Path) -> list[Violation]:
    """Check everything below ``root`` against :data:`POLICY`.

    The single implementation of every rule. Both the committed-tree cases and
    the synthetic firing cases call it, so a rule cannot be proved to fire by one
    code path while the real corpus is checked by another.

    Args:
        root: The corpus root to check.

    Returns:
        Every violation found, sorted. An empty list means the corpus conforms.
    """
    suffix = POLICY["sidecar_suffix"]
    violations: list[Violation] = []

    for path in corpus_files(root):
        relative = path.relative_to(root).as_posix()
        tier = _tier_of(path, root)
        extension = _extension_of(path)

        # Reported rather than skipped: a sweep that only looks inside the tier
        # directories cannot tell a file nobody classified from one nobody
        # added, and an unclassified file is one no provenance rule reaches.
        if tier is None:
            violations.append(
                Violation("unknown_tier", relative, "not in a tier directory")
            )
        # `README.md` is exempt by name rather than `.md` by extension,
        # which **narrows** the provenance-free slot rather than closing it: a
        # Markdown file needs no sidecar under the pairing rule, so a capture
        # pasted into a file called `README.md` still names no source, no date
        # and no licence. What the exemption buys is that the slot is one known
        # filename per tier directory instead of every `.md` anybody adds, so a
        # second one cannot appear without widening `allowed_filenames` -- a
        # reviewed, two-file edit. The size cap, the sanitation sweep and the
        # encoding rules do apply to it; only provenance does not -- and each
        # of those three is pinned by a case below, because a promise made in a
        # comment is the natural thing to carve away when somebody wants a
        # Markdown page this table rejects.
        if (
            path.name not in POLICY["allowed_filenames"]
            and extension not in POLICY["allowed_extensions"]
        ):
            violations.append(
                Violation("disallowed_extension", relative, extension or "(none)")
            )

        raw = path.read_bytes()
        if tier is not None and len(raw) > POLICY["tiers"][tier]["max_bytes"]:
            violations.append(
                Violation(
                    "over_size_cap",
                    relative,
                    f"{len(raw)} > {POLICY['tiers'][tier]['max_bytes']}",
                )
            )
        # CRLF is reported rather than tolerated because two other rules are
        # measured in bytes: a corpus checked out with CRLF has a different size
        # on every line and a different golden on every platform.
        if b"\r\n" in raw:
            violations.append(
                Violation("crlf_line_ending", relative, "corpus files are LF-only")
            )

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            # The sanitation sweep still runs, on a lossy decode: a file this
            # guard cannot read cleanly is exactly the one whose contents nobody
            # has reviewed.
            violations.append(Violation("undecodable", relative, str(error)))
            text = raw.decode("utf-8", errors="replace")

        violations.extend(_sanitation_violations(text, relative))

        if extension == suffix:
            violations.extend(_sidecar_violations(path, relative, tier, text, suffix))
        elif extension == ".html":
            violations.extend(_page_violations(path, relative, tier, text, suffix))

    violations.extend(_required_fragment_violations(root))
    return sorted(violations)


def rows_tripped_by(text: str) -> list[str]:
    """Return the ids of every sanitation row the text matches, sorted.

    Scanned twice: as written, and with character references resolved. A
    published address is most often written `info&#64;example.com`, which is
    inert to a pattern reading raw bytes and a live mailto to every browser
    that renders the file. The two passes are folded into one result per row,
    so an address written both ways is still one violation.

    The single matcher, shared by :func:`check_corpus` and by the cases that
    calibrate the specimens -- a specificity case reading raw text only would
    disagree with the checker about which rows a specimen trips.

    Args:
        text: The text to scan.

    Returns:
        The matching row ids.
    """
    candidates = (text, unescape(text))
    return sorted(
        row_id
        for row_id, row in POLICY["sanitation_patterns"].items()
        if any(re.search(row["regex"], candidate) for candidate in candidates)
    )


def _sanitation_violations(text: str, relative: str) -> list[Violation]:
    """Return one violation per sanitation row the text matches.

    Args:
        text: The file's decoded contents.
        relative: The file's path, relative to the corpus root.

    Returns:
        A violation per matching row, identified by row id. The matched text is
        deliberately not carried: a failure message reaches a CI log, and
        printing the credential would publish the thing the rule exists to keep
        unpublished.
    """
    return [
        Violation("sanitation", relative, row_id) for row_id in rows_tripped_by(text)
    ]


def _page_violations(
    path: Path, relative: str, tier: str | None, text: str, suffix: str
) -> list[Violation]:
    """Return the violations that apply to an HTML page.

    Args:
        path: The page.
        relative: Its path relative to the corpus root.
        tier: The tier it belongs to, or ``None``.
        text: Its decoded contents.
        suffix: The sidecar suffix from the policy.

    Returns:
        A pairing violation when its sidecar is absent, and -- for a fragment --
        a length violation when it carries too little prose.
    """
    violations: list[Violation] = []

    sidecar = path.with_name(f"{path.stem}{suffix}")
    if not sidecar.exists():
        violations.append(Violation("missing_sidecar", relative, sidecar.name))

    # Fragments only, and the floor is a measured margin rather than a
    # threshold. `extract_content_as_markdown` selects its BeautifulSoup
    # fallback on a *falsy* trafilatura result -- there is no exception handling
    # around that call -- and on trafilatura 2.2.0 with production's exact
    # arguments the result went falsy below roughly 112 extracted characters.
    # Lowering MIN_EXTRACTED_SIZE moved that cliff down; raising it did not move
    # it up, so 250 is not the cliff, it is the documented setting nearest it and
    # about 2.2x the measured one. A golden taken from a shorter fragment would
    # pin the fallback rather than the path a real fetch takes: measured, at 10
    # and 37 extracted characters production output was byte-identical to
    # `_bs4_markdownify_fallback`, and at 627 to trafilatura's.
    if tier == "fragments":
        length = len(visible_text(text))
        floor = POLICY["min_fragment_text_chars"]
        if length < floor:
            violations.append(
                Violation("fragment_text_too_short", relative, f"{length} < {floor}")
            )

    return violations


def _sidecar_violations(
    path: Path, relative: str, tier: str | None, text: str, suffix: str
) -> list[Violation]:
    """Return the violations that apply to a provenance sidecar.

    Args:
        path: The sidecar.
        relative: Its path relative to the corpus root.
        tier: The tier it belongs to, or ``None`` when it is outside both tier
            directories -- in which case the field rules are skipped, because
            there is no tier whose field list would apply and the unclassified
            location is already reported.
        text: Its decoded contents, read once by the caller.
        suffix: The sidecar suffix from the policy.

    Returns:
        A pairing violation when the page it describes is gone, a shape
        violation when it is not a JSON object, and otherwise its tier's field
        violations.
    """
    violations: list[Violation] = []

    page = path.with_name(f"{path.name[: -len(suffix)]}.html")
    if not page.exists():
        violations.append(Violation("orphan_sidecar", relative, page.name))

    # Parsed and shape-checked in one place: "parseable" is not "usable". A JSON
    # list supports `in` and iterates as its own elements, so a field check
    # written without this reports every field missing -- or, on a list of the
    # right strings, reports none missing, which is worse.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        return [*violations, Violation("unreadable_sidecar", relative, str(error))]
    if not isinstance(data, dict):
        return [
            *violations,
            Violation(
                "unreadable_sidecar", relative, f"not an object: {type(data).__name__}"
            ),
        ]

    if tier is None:
        return violations
    return violations + _field_violations(data, relative, tier)


def _field_violations(
    data: dict[str, Any], relative: str, tier: str
) -> list[Violation]:
    """Return the provenance-field violations for one parsed sidecar.

    Args:
        data: The parsed sidecar object.
        relative: The sidecar's path relative to the corpus root.
        tier: The tier whose field lists apply.

    Returns:
        One violation per broken field rule.
    """
    spec = POLICY["tiers"][tier]
    violations: list[Violation] = []

    # A required field must be a non-blank string. `"   "` satisfies a presence
    # check written with `in` and records nothing at all.
    for field in spec["required"]:
        if not _is_filled(data.get(field)):
            violations.append(Violation("missing_field", relative, field))

    # "Licence or rationale": one is enough, and requiring both would be wrong in
    # the direction that costs a contributor an afternoon.
    any_of = spec["required_any_of"]
    if any_of and not any(_is_filled(data.get(field)) for field in any_of):
        violations.append(Violation("missing_any_of", relative, ", ".join(any_of)))

    # Presence, not truthiness of the value: a fragment carrying an explicit
    # `"source_url": null` is still a fragment claiming a capture provenance.
    for field in spec["forbidden"]:
        if field in data:
            violations.append(Violation("forbidden_field", relative, field))

    # A field its tier forbids is skipped here, so one defect produces one
    # violation rather than a forbidden-field and a malformed-field report for
    # the same key.
    #
    # `fullmatch`, not `search`: without `re.MULTILINE`, Python's `$` also
    # matches immediately before a final newline, so `re.search` accepted
    # `"2026-01-02\n"` -- and `"https://example.com/x\n"` too, since `\S+`
    # simply stops at the newline. `_is_filled` passes such a value as well,
    # because `strip()` removes it. Measured: `search` True and `fullmatch`
    # False for both patterns.
    for field, pattern in POLICY["field_patterns"].items():
        if field in spec["forbidden"]:
            continue
        value = data.get(field)
        if _is_filled(value) and not re.fullmatch(pattern, value):
            violations.append(Violation("malformed_field", relative, field))

    return violations


def _required_fragment_violations(root: Path) -> list[Violation]:
    """Return one violation per named fragment that is not committed.

    A count would not do. The design document names four transformations by the
    behaviour each exercises, and a floor of "at least three files" is satisfied
    by three copies of the same table -- silently removing the input a later
    step needs for entity handling.

    Args:
        root: The corpus root.

    Returns:
        A violation per absent required fragment.
    """
    present = {path.stem for path in (root / "fragments").glob("*.html")}
    return [
        Violation("missing_required_fragment", "fragments", stem)
        for stem in POLICY["required_fragments"]
        if stem not in present
    ]


def _is_filled(value: Any) -> TypeGuard[str]:
    """Return whether a sidecar value counts as supplied.

    Args:
        value: The value read from the sidecar, of any JSON type.

    Returns:
        ``True`` only for a string with non-whitespace content. A number, a
        ``null`` and a blank string all record nothing a reader could act on.
        Typed as a :class:`typing.TypeGuard` so the pattern check below narrows
        the value to ``str`` rather than passing ``Any`` to :func:`re.search`.
    """
    return isinstance(value, str) and bool(value.strip())


# ---------------------------------------------------------------------------
# Document agreement
# ---------------------------------------------------------------------------


def test_the_design_document_declares_the_same_policy() -> None:
    """Assert section 3.3 and :data:`POLICY` are equal, both directions

    Compared as parsed objects rather than as text, so reformatting the block
    does not fail the guard while changing a value does. Editing either side
    alone turns the suite red, which is what keeps the design document
    describing the policy that actually runs.
    """
    documented = _design_document_policy()
    assert documented == POLICY, (
        f"section 3.3 of {TEST_SUITE_PATH.name} and POLICY in "
        f"{Path(__file__).name} disagree. Documented but not held: "
        f"{ {k: v for k, v in documented.items() if POLICY.get(k) != v} }. "
        f"Held but not documented: "
        f"{ {k: v for k, v in POLICY.items() if documented.get(k) != v} }. "
        "Change both in the same pull request."
    )


def test_a_renamed_section_heading_fails_naming_the_heading() -> None:
    """Assert a rename is reported as a rename, not as an index error

    ``_section_body`` finds its heading with ``list.index``, which raises a bare
    ``ValueError`` naming nothing. The guard against a silent disable-by-rename
    was itself unguarded: measured, deleting the assert left the module green.
    """
    with pytest.raises(AssertionError, match="is not a heading"):
        _design_section("### 3.3 Renamed By Somebody")


def test_a_second_json_block_fails_with_the_count() -> None:
    """Assert two blocks are a structural failure, reported with the count

    Driven through :func:`_design_document_policy` rather than by re-running the
    regular expression over the real document: the case below proves the
    *document* has one block, which is a different claim from the helper
    refusing two. Measured -- relaxing the helper to ``>= 1`` left the module
    green.
    """
    with pytest.raises(AssertionError, match="holds 2 fenced json blocks"):
        _design_document_policy("```json\n{}\n```\n```json\n{}\n```")


def test_the_section_holds_exactly_one_fenced_json_block() -> None:
    """Assert the policy has one home in the document

    A second block under this heading would make "the policy" ambiguous, and the
    guard would pick one of them by position. Failing here says so directly.
    """
    blocks = re.findall(r"```json\n(.*?)```", _design_section(), re.DOTALL)
    assert len(blocks) == 1, f"expected one fenced json block, found {len(blocks)}"


def test_every_mandated_sanitation_category_is_present() -> None:
    """Assert the plan step's named categories all still have a row

    :data:`POLICY` and the design document are edited together, so this literal
    is the third edit standing between a deleted row and a silently smaller
    sweep.
    """
    missing = sorted(MANDATED_SANITATION_ROWS - set(POLICY["sanitation_patterns"]))
    assert not missing, (
        f"the sanitation table no longer covers {missing}; those categories are "
        "promised by the implementation plan's verify clause for this step."
    )


def test_the_provenance_field_sets_are_pinned_against_literals() -> None:
    """Assert the tiers and their field lists still say what was promised

    Every parametrized field sweep in this module draws its cases from
    :data:`POLICY`, so removing ``capture_date`` from the policy removes the case
    that would have noticed. These literals are what fails instead.
    """
    assert set(POLICY["tiers"]) == MANDATED_TIERS
    snapshots = POLICY["tiers"]["snapshots"]
    assert tuple(snapshots["required"]) == MANDATED_SNAPSHOT_REQUIRED
    assert tuple(snapshots["required_any_of"]) == MANDATED_SNAPSHOT_ANY_OF
    assert (
        tuple(POLICY["tiers"]["fragments"]["forbidden"]) == MANDATED_FRAGMENT_FORBIDDEN
    )
    assert set(POLICY["field_patterns"]) == MANDATED_PATTERNED_FIELDS
    assert {
        tier: spec["max_bytes"] for tier, spec in POLICY["tiers"].items()
    } == MANDATED_TIER_CAPS
    assert tuple(POLICY["required_fragments"]) == MANDATED_REQUIRED_FRAGMENTS


def test_every_sanitation_row_carries_both_kinds_of_specimen() -> None:
    """Assert no row can be disarmed by emptying its specimen lists

    Every firing case below is generated from these lists, so emptying one in
    both copies of the policy deletes the cases that would have noticed.
    Measured by collecting the module with and without them: emptying
    ``bearer_token``'s positives removes exactly three cases, and before this
    case existed the remainder stayed green. The drop is recorded rather than a
    pair of totals -- a total is stale the next time a case is added, and this
    is the module whose whole subject is a number that drifts the moment one of
    its copies is edited.
    """
    for row_id, row in sorted(POLICY["sanitation_patterns"].items()):
        for kind in ("matches", "does_not_match"):
            assert isinstance(row[kind], list) and row[kind], (
                f"sanitation row {row_id!r} has no {kind} specimen, so nothing "
                "drives it"
            )


#: Every (row, positive specimen) pair, driven one at a time. A page carrying all
#: of a row's specimens at once is caught by any single surviving shape, so a row
#: whose header form works and whose two in-document forms are dead would pass.
SANITATION_MATCH_CASES = [
    pytest.param(row_id, specimen, id=f"{row_id}-{index}")
    for row_id, row in sorted(POLICY["sanitation_patterns"].items())
    for index, specimen in enumerate(row["matches"])
]

#: Every (row, negative specimen) pair.
SANITATION_MISS_CASES = [
    pytest.param(row_id, specimen, id=f"{row_id}-{index}")
    for row_id, row in sorted(POLICY["sanitation_patterns"].items())
    for index, specimen in enumerate(row["does_not_match"])
]


@pytest.mark.parametrize(("row_id", "specimen"), SANITATION_MATCH_CASES)
def test_each_sanitation_specimen_identifies_exactly_its_own_row(
    row_id: str, specimen: str
) -> None:
    """Assert a positive specimen trips its own row and no other

    Every firing case below feeds these specimens to the checker and asserts a
    single named violation. Without this, such a case could be green while
    asserting a different row's result.
    """
    tripped = rows_tripped_by(specimen)
    assert tripped == [row_id], f"the specimen trips {tripped}, not just {row_id!r}"


@pytest.mark.parametrize(("row_id", "specimen"), SANITATION_MISS_CASES)
def test_no_sanitation_row_trips_on_a_negative_specimen(
    row_id: str, specimen: str
) -> None:
    """Assert ordinary prose near a row's topic trips nothing at all

    An over-broad pattern is not a stricter guard, it is a deleted one: a rule
    that reports every page in the corpus gets removed, and the credential it
    was watching for goes unwatched. Asserted across the whole table rather than
    against ``row_id`` alone, because a specimen written to clear one row may
    still trip another.
    """
    tripped = rows_tripped_by(specimen)
    assert not tripped, f"{row_id!r}'s negative specimen trips {tripped}"


# ---------------------------------------------------------------------------
# The committed tree
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def committed_violations() -> list[Violation]:
    """Check the real corpus once and share the result across the cases.

    Returns:
        Every violation in the committed corpus.
    """
    return check_corpus(CORPUS_ROOT)


def _of_rule(violations: list[Violation], *rules: str) -> list[Violation]:
    """Return the violations of the named rules.

    Args:
        violations: The full result of a check.
        *rules: The rule identifiers to filter on.

    Returns:
        Only the violations of those rules, keeping the order of ``violations``.
    """
    return [violation for violation in violations if violation.rule in rules]


def test_the_corpus_root_exists_and_holds_both_tier_directories() -> None:
    """Assert the layout the policy is written against is actually there

    Checked before the sweeps below, because every one of them passes trivially
    against a directory that does not exist.
    """
    assert CORPUS_ROOT.is_dir(), f"{CORPUS_ROOT} is missing"
    for tier in POLICY["tiers"]:
        assert (CORPUS_ROOT / tier).is_dir(), f"tier directory {tier!r} is missing"


def test_the_committed_corpus_sweep_is_not_looking_at_nothing() -> None:
    """Assert the committed sweep has files to sweep

    Every per-rule case below reports an empty list against an empty corpus. This
    is the case that fails instead.
    """
    found = corpus_files(CORPUS_ROOT)
    expected = 2 * len(POLICY["required_fragments"])
    assert len(found) >= expected, (
        f"the corpus sweep found {len(found)} files; the {len(POLICY['required_fragments'])} "
        f"required fragments alone mean at least {expected} once each has its sidecar."
    )


def test_every_committed_file_matches_no_sanitation_pattern(
    committed_violations: list[Violation],
) -> None:
    """Assert no committed file matches a sanitation pattern

    The case this whole module exists for. A failure here means a credential, an
    address or a tracking script is about to be published, or already has been.
    """
    found = _of_rule(committed_violations, "sanitation")
    assert not found, f"committed corpus files match sanitation patterns: {found}"


def test_every_required_fragment_is_committed(
    committed_violations: list[Violation],
) -> None:
    """Assert each named handcrafted fragment exists"""
    found = _of_rule(committed_violations, "missing_required_fragment")
    assert not found, f"required fragments are not committed: {found}"


def test_every_committed_fragment_carries_enough_prose(
    committed_violations: list[Violation],
) -> None:
    """Assert each fragment is long enough to reach the production extractor"""
    found = _of_rule(committed_violations, "fragment_text_too_short")
    assert not found, f"fragments below the extraction floor: {found}"


def test_every_committed_page_has_a_sidecar(
    committed_violations: list[Violation],
) -> None:
    """Assert every committed HTML file has its provenance sidecar"""
    found = _of_rule(committed_violations, "missing_sidecar")
    assert not found, f"committed corpus pages with no sidecar: {found}"


def test_no_committed_sidecar_is_an_orphan(
    committed_violations: list[Violation],
) -> None:
    """Assert every committed sidecar still has the page it describes"""
    found = _of_rule(committed_violations, "orphan_sidecar")
    assert not found, f"committed sidecars with no page: {found}"


def test_every_committed_file_is_within_its_tiers_size_cap(
    committed_violations: list[Violation],
) -> None:
    """Assert no committed file exceeds its tier's byte cap"""
    found = _of_rule(committed_violations, "over_size_cap")
    assert not found, f"committed corpus files over the size cap: {found}"


def test_every_committed_file_is_utf8_with_lf_endings(
    committed_violations: list[Violation],
) -> None:
    """Assert the corpus is byte-identical on every platform

    Two rules are measured in bytes -- the size cap, and the goldens a later
    step takes from these fragments. A CRLF checkout changes both.
    """
    found = _of_rule(committed_violations, "undecodable", "crlf_line_ending")
    assert not found, f"committed corpus files are not UTF-8 with LF endings: {found}"


def test_every_committed_sidecar_is_readable_json(
    committed_violations: list[Violation],
) -> None:
    """Assert every committed sidecar parses as a JSON object"""
    found = _of_rule(committed_violations, "unreadable_sidecar")
    assert not found, f"committed sidecars that do not parse: {found}"


def test_every_committed_sidecar_carries_its_tiers_fields(
    committed_violations: list[Violation],
) -> None:
    """Assert no committed sidecar is missing a field its tier requires"""
    found = _of_rule(committed_violations, "missing_field", "missing_any_of")
    assert not found, f"committed sidecars missing required provenance: {found}"


def test_no_committed_sidecar_carries_a_field_its_tier_forbids(
    committed_violations: list[Violation],
) -> None:
    """Assert no fragment claims a capture provenance

    A fragment naming a source URL is a snapshot filed in the wrong directory.
    """
    found = _of_rule(committed_violations, "forbidden_field")
    assert not found, f"committed sidecars carrying a forbidden field: {found}"


def test_every_committed_provenance_field_is_well_formed(
    committed_violations: list[Violation],
) -> None:
    """Assert no committed sidecar satisfies a field with a placeholder

    ``"n/a"`` and ``"soon"`` both pass "present and non-empty" while defeating
    the point of recording provenance at all.

    **Structurally vacuous on today's corpus**, and not merely unexercised: both
    patterned fields are on the fragments tier's forbidden list, and the field
    check skips a forbidden key, so with no snapshot committed not one pattern
    is evaluated here. The rule is carried entirely by
    :func:`test_a_placeholder_provenance_value_is_reported` until a snapshot
    lands.
    """
    found = _of_rule(committed_violations, "malformed_field")
    assert not found, f"committed sidecars with malformed provenance: {found}"


def test_every_committed_file_sits_in_a_known_tier_with_an_allowed_extension(
    committed_violations: list[Violation],
) -> None:
    """Assert no corpus file escaped the tier directories or the extension list"""
    found = _of_rule(committed_violations, "unknown_tier", "disallowed_extension")
    assert not found, f"corpus files outside the declared layout: {found}"


def test_no_python_module_lives_under_the_corpus_directory() -> None:
    """Assert nothing under the corpus is importable

    The stronger half of the collection claim below, and the one that keeps
    holding when pytest's collection rules change. Checked over the tree rather
    than by running pytest, so it is not confounded by a configuration that
    happens to deselect.
    """
    modules = sorted(str(p.relative_to(REPO_ROOT)) for p in CORPUS_TREE.rglob("*.py"))
    assert not modules, f"the corpus holds python modules: {modules}"


@pytest.mark.subsystem
def test_pytest_collects_nothing_from_the_corpus_directory() -> None:
    """Assert the corpus is data to this suite, never tests

    ``tests/`` is a collection root. Nothing about ``.html`` invites collection
    today, and this case is what keeps that true the day somebody adds a
    ``conftest.py`` in there -- at which point the corpus starts executing at
    collection time.

    Run as a child process with a cleaned environment, following the shape the
    other collection guards in this suite use.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            str(CORPUS_TREE),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        env={
            **{k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"},
            "PYTHONIOENCODING": "utf-8",
        },
        cwd=str(REPO_ROOT),
    )

    # Exit code 5 is "no tests collected", the expected outcome; the substring
    # check is what distinguishes it from a collection *error*, which also
    # collects nothing.
    assert completed.returncode == 5, (
        f"expected an empty collection, got exit {completed.returncode}\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "error" not in completed.stdout.lower(), completed.stdout


# ---------------------------------------------------------------------------
# That the policy fires
# ---------------------------------------------------------------------------

#: A conforming fragment sidecar, copied and then broken by the firing cases.
CONFORMING_FRAGMENT_SIDECAR: dict[str, Any] = {
    "rationale": "Synthetic conforming fragment, written by the policy guard."
}

#: A conforming snapshot sidecar. Carries ``licence`` and not ``rationale`` so
#: the ``required_any_of`` cases have one alternative present and one absent to
#: work from.
CONFORMING_SNAPSHOT_SIDECAR: dict[str, Any] = {
    "source_url": "https://example.com/a-page",
    "capture_date": "2026-01-02",
    "licence": "CC BY 4.0",
}

#: The body every synthetic page gets unless a case replaces it. Deliberately
#: dull -- it must trip no sanitation row, or every firing case below would carry
#: a second violation it did not ask for -- and deliberately over the fragment
#: prose floor, so a fragment written from it conforms.
CONFORMING_PAGE = (
    "<article><h2>Synthetic page</h2><p>"
    + ("This paragraph exists to carry enough prose for the fragment floor. " * 6)
    + "</p></article>\n"
)

#: The stem each tier's synthetic page uses, so a parametrized case can address
#: the right file from the tier name alone.
TIER_STEM = {"fragments": POLICY["required_fragments"][0], "snapshots": "snapshot"}

#: The conforming sidecar for each tier.
TIER_SIDECAR = {
    "fragments": CONFORMING_FRAGMENT_SIDECAR,
    "snapshots": CONFORMING_SNAPSHOT_SIDECAR,
}


def _write_page(directory: Path, stem: str, body: str, sidecar: Any) -> None:
    """Write one synthetic page and its sidecar.

    Args:
        directory: The tier directory to write into; created if absent.
        stem: The file name without its extension.
        body: The HTML body to write.
        sidecar: The sidecar object to serialize, or ``None`` to write no
            sidecar at all.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.html").write_text(body, encoding="utf-8", newline="\n")
    if sidecar is not None:
        (directory / f"{stem}{POLICY['sidecar_suffix']}").write_text(
            json.dumps(sidecar, indent=2), encoding="utf-8", newline="\n"
        )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Build a synthetic corpus that conforms to the policy in every respect.

    Every firing case starts from this and applies exactly one defect, which is
    what lets each of them assert an exact violation list rather than a
    non-empty one. Every required fragment is present, so a case that breaks one
    file does not also trip the required-fragment rule.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The root of a conforming synthetic corpus.
    """
    root = tmp_path / "html"
    for stem in POLICY["required_fragments"]:
        _write_page(
            root / "fragments", stem, CONFORMING_PAGE, CONFORMING_FRAGMENT_SIDECAR
        )
    _write_page(
        root / "snapshots", "snapshot", CONFORMING_PAGE, CONFORMING_SNAPSHOT_SIDECAR
    )
    return root


def _sidecar_path(corpus_root: Path, tier: str, stem: str) -> Path:
    """Return the path of one synthetic sidecar.

    Args:
        corpus_root: The synthetic corpus root.
        tier: The tier directory name.
        stem: The page's file name without its extension.

    Returns:
        The sidecar path.
    """
    return corpus_root / tier / f"{stem}{POLICY['sidecar_suffix']}"


def _rewrite_sidecar(path: Path, data: Any) -> None:
    """Replace one sidecar's contents.

    Args:
        path: The sidecar to rewrite.
        data: The object to serialize in its place.
    """
    path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")


def test_a_conforming_synthetic_corpus_has_no_violations(corpus: Path) -> None:
    """Assert the checker reports nothing against conforming input

    The control for every case below. Without it, a checker that reported a
    spurious violation on every file would satisfy each firing case's "exactly
    one violation" assertion only by accident, and a checker that reported
    nothing would fail them all for a reason nobody could localize.
    """
    assert check_corpus(corpus) == []


def test_a_page_with_no_sidecar_is_reported(corpus: Path) -> None:
    """Assert an HTML file with no provenance sidecar violates the policy"""
    _sidecar_path(corpus, "snapshots", "snapshot").unlink()

    assert check_corpus(corpus) == [
        Violation("missing_sidecar", "snapshots/snapshot.html", "snapshot.meta.json")
    ]


def test_a_sidecar_with_no_page_is_reported(corpus: Path) -> None:
    """Assert a sidecar left behind by a deleted page violates the policy

    The reverse of the rule above, and not implied by it: a check that only walks
    HTML files cannot see a sidecar nothing points at.
    """
    (corpus / "snapshots" / "snapshot.html").unlink()

    assert check_corpus(corpus) == [
        Violation("orphan_sidecar", "snapshots/snapshot.meta.json", "snapshot.html")
    ]


def test_a_required_fragment_that_is_not_committed_is_reported(corpus: Path) -> None:
    """Assert a named fragment cannot be dropped silently

    The page and its sidecar are removed together, which is what a real deletion
    looks like; removing only the page would report the orphan instead.
    """
    stem = POLICY["required_fragments"][-1]
    (corpus / "fragments" / f"{stem}.html").unlink()
    _sidecar_path(corpus, "fragments", stem).unlink()

    assert check_corpus(corpus) == [
        Violation("missing_required_fragment", "fragments", stem)
    ]


def test_a_fragment_with_too_little_prose_is_reported(corpus: Path) -> None:
    """Assert a fragment below the extraction floor is reported

    A shorter fragment silently pins this project's BeautifulSoup fallback
    rather than the path a real fetch takes -- measured at 10 and 37 extracted
    characters, where production output was byte-identical to the fallback's.
    """
    stem = POLICY["required_fragments"][0]
    (corpus / "fragments" / f"{stem}.html").write_text(
        "<article><p>Too short.</p></article>\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [
        Violation(
            "fragment_text_too_short",
            f"fragments/{stem}.html",
            f"10 < {POLICY['min_fragment_text_chars']}",
        )
    ]


def test_markup_and_script_bodies_do_not_count_towards_the_prose_floor(
    corpus: Path,
) -> None:
    """Assert the prose measure ignores tags, scripts and styles

    A fragment padded to the floor with attributes or an inline script would
    still extract to nothing, so measuring raw length would make the rule
    satisfiable without satisfying it.
    """
    stem = POLICY["required_fragments"][0]
    padding = "<span class='{}'></span>".format("p" * 400)
    (corpus / "fragments" / f"{stem}.html").write_text(
        f"<article>{padding}<script>var x = '{'y' * 400}';</script>"
        "<p>Short.</p></article>\n",
        encoding="utf-8",
        newline="\n",
    )

    assert [v.rule for v in check_corpus(corpus)] == ["fragment_text_too_short"]


@pytest.mark.parametrize("tier", sorted(POLICY["tiers"]))
def test_a_page_one_byte_over_its_tiers_cap_is_reported(
    corpus: Path, tier: str
) -> None:
    """Assert each tier's size cap fires at its own ``max_bytes + 1``

    Driven per tier because the caps differ: a single case against the larger
    cap would pass unchanged if the fragment cap were deleted.
    """
    cap = POLICY["tiers"][tier]["max_bytes"]
    stem = TIER_STEM[tier]
    body = "<article><p>" + "x" * cap + "</p></article>"
    (corpus / tier / f"{stem}.html").write_text(
        body[: cap + 1], encoding="utf-8", newline="\n"
    )

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("over_size_cap", f"{tier}/{stem}.html")
    ]


@pytest.mark.parametrize("tier", sorted(POLICY["tiers"]))
def test_a_page_exactly_at_its_tiers_cap_is_accepted(corpus: Path, tier: str) -> None:
    """Assert each cap is inclusive

    Paired with the case above so the boundary is pinned from both sides; a
    single over-cap case passes just as well against ``>=``.
    """
    cap = POLICY["tiers"][tier]["max_bytes"]
    stem = TIER_STEM[tier]
    filler = "This is prose that carries the fragment past its floor. "
    body = "<article><p>" + filler * (cap // len(filler) + 2) + "</p></article>"
    (corpus / tier / f"{stem}.html").write_text(
        body[:cap], encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == []


def test_a_file_with_a_disallowed_extension_is_reported(corpus: Path) -> None:
    """Assert only the declared extensions may live in the corpus

    Written as clean ASCII text so the case reports the extension rule and
    nothing else; a binary file would also, correctly, be reported as
    undecodable.
    """
    (corpus / "fragments" / "notes.txt").write_text(
        "A stray file.\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [
        Violation("disallowed_extension", "fragments/notes.txt", ".txt")
    ]


def test_a_file_that_is_not_utf8_is_reported(corpus: Path) -> None:
    """Assert an undecodable capture is named rather than crashing the sweep

    Real captures are frequently windows-1252 or shift_jis. A guard that raised
    here would report nothing about the rest of the corpus and would name
    neither the rule nor the remedy.
    """
    (corpus / "snapshots" / "snapshot.html").write_bytes(
        b"<article><p>caf\xe9 stands here for a windows-1252 capture.</p></article>\n"
    )

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("undecodable", "snapshots/snapshot.html")
    ]


def test_a_file_with_crlf_line_endings_is_reported(corpus: Path) -> None:
    """Assert the corpus is LF-only

    The size cap and the goldens a later step takes from these fragments are
    both measured in bytes, and a CRLF checkout changes both.
    """
    (corpus / "snapshots" / "snapshot.html").write_bytes(
        CONFORMING_PAGE.replace("\n", "\r\n").encode("utf-8")
    )

    assert check_corpus(corpus) == [
        Violation(
            "crlf_line_ending", "snapshots/snapshot.html", "corpus files are LF-only"
        )
    ]


def test_an_unparseable_sidecar_is_reported(corpus: Path) -> None:
    """Assert a sidecar that is not JSON is reported rather than raising

    A checker that raised here would take the whole guard down with one bad file
    and report nothing about the rest of the corpus.
    """
    _sidecar_path(corpus, "snapshots", "snapshot").write_text("{", encoding="utf-8")

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("unreadable_sidecar", "snapshots/snapshot.meta.json")
    ]


def test_a_sidecar_that_is_not_an_object_is_reported(corpus: Path) -> None:
    """Assert a JSON list in a sidecar is reported, not indexed

    Parseable but wrong-shaped is a distinct case from unparseable: a list
    supports ``in`` and walks as its own elements, so a field check written
    without this would quietly find every field missing -- or, on a list of the
    right strings, none missing, which is worse.
    """
    _rewrite_sidecar(
        _sidecar_path(corpus, "snapshots", "snapshot"), ["not", "an object"]
    )

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("unreadable_sidecar", "snapshots/snapshot.meta.json")
    ]


#: Every (tier, required field) pair the policy declares, driven one at a time.
REQUIRED_FIELD_CASES = [
    (tier, field)
    for tier, spec in sorted(POLICY["tiers"].items())
    for field in spec["required"]
]


@pytest.mark.parametrize(("tier", "field"), REQUIRED_FIELD_CASES)
def test_a_sidecar_missing_one_required_field_is_reported(
    corpus: Path, tier: str, field: str
) -> None:
    """Assert each required field is required, one field at a time

    Removing the whole sidecar would prove only that *something* is checked. One
    field at a time is what distinguishes a field that is enforced from one that
    is merely listed.
    """
    stem = TIER_STEM[tier]
    data = {k: v for k, v in TIER_SIDECAR[tier].items() if k != field}
    _rewrite_sidecar(_sidecar_path(corpus, tier, stem), data)

    assert check_corpus(corpus) == [
        Violation("missing_field", f"{tier}/{stem}{POLICY['sidecar_suffix']}", field)
    ]


@pytest.mark.parametrize(("tier", "field"), REQUIRED_FIELD_CASES)
def test_a_required_field_present_but_blank_is_reported(
    corpus: Path, tier: str, field: str
) -> None:
    """Assert a whitespace-only value does not satisfy a required field

    ``{"source_url": "   "}`` satisfies a presence check written with ``in`` and
    records nothing at all.
    """
    stem = TIER_STEM[tier]
    data = {**TIER_SIDECAR[tier], field: "   "}
    _rewrite_sidecar(_sidecar_path(corpus, tier, stem), data)

    assert check_corpus(corpus) == [
        Violation("missing_field", f"{tier}/{stem}{POLICY['sidecar_suffix']}", field)
    ]


def test_a_snapshot_with_neither_licence_nor_rationale_is_reported(
    corpus: Path,
) -> None:
    """Assert the licence-or-rationale requirement fires when both are absent"""
    data = {k: v for k, v in CONFORMING_SNAPSHOT_SIDECAR.items() if k != "licence"}
    _rewrite_sidecar(_sidecar_path(corpus, "snapshots", "snapshot"), data)

    assert check_corpus(corpus) == [
        Violation("missing_any_of", "snapshots/snapshot.meta.json", "licence, rationale")
    ]


@pytest.mark.parametrize("field", POLICY["tiers"]["snapshots"]["required_any_of"])
def test_either_licence_or_rationale_alone_satisfies_the_requirement(
    corpus: Path, field: str
) -> None:
    """Assert each alternative is genuinely sufficient on its own

    Without this, an implementation that required *both* would pass the case
    above and be wrong in the direction that costs a contributor an afternoon.
    """
    data = {k: v for k, v in CONFORMING_SNAPSHOT_SIDECAR.items() if k != "licence"}
    data[field] = "A reason a human wrote."
    _rewrite_sidecar(_sidecar_path(corpus, "snapshots", "snapshot"), data)

    assert check_corpus(corpus) == []


@pytest.mark.parametrize("field", POLICY["tiers"]["fragments"]["forbidden"])
def test_a_fragment_claiming_a_capture_provenance_is_reported(
    corpus: Path, field: str
) -> None:
    """Assert a fragment may not carry a snapshot's provenance fields

    This rule is what makes a captured page parked under ``fragments/`` visible
    when its provenance is kept. It does **not** catch a fresh commit that never
    named a source -- see section 3.3, where the per-tier size cap is what
    stands in for that.
    """
    values = {"source_url": "https://example.com/x", "capture_date": "2026-01-02"}
    data = {**CONFORMING_FRAGMENT_SIDECAR, field: values[field]}
    stem = TIER_STEM["fragments"]
    _rewrite_sidecar(_sidecar_path(corpus, "fragments", stem), data)

    assert check_corpus(corpus) == [
        Violation(
            "forbidden_field", f"fragments/{stem}{POLICY['sidecar_suffix']}", field
        )
    ]


@pytest.mark.parametrize("field", POLICY["tiers"]["fragments"]["forbidden"])
def test_a_forbidden_field_with_a_malformed_value_is_reported_once(
    corpus: Path, field: str
) -> None:
    """Assert a field its tier forbids is not also reported as malformed

    One defect, one violation. The case above supplies a *well-formed* value, so
    it cannot tell the pattern skip from its absence -- measured, deleting the
    skip left the module green.
    """
    data = {**CONFORMING_FRAGMENT_SIDECAR, field: "n/a"}
    stem = TIER_STEM["fragments"]
    _rewrite_sidecar(_sidecar_path(corpus, "fragments", stem), data)

    assert check_corpus(corpus) == [
        Violation(
            "forbidden_field", f"fragments/{stem}{POLICY['sidecar_suffix']}", field
        )
    ]


def test_a_markdown_file_that_is_not_a_readme_is_reported(corpus: Path) -> None:
    """Assert Markdown is admitted by name, not by extension

    A blanket ``.md`` allowance would be a provenance-free slot: the pairing
    rule asks for a sidecar only beside an HTML page, so a capture pasted in as
    Markdown would name no source, no date and no licence.
    """
    (corpus / "snapshots" / "pasted.md").write_text(
        "# A capture with no provenance\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [
        Violation("disallowed_extension", "snapshots/pasted.md", ".md")
    ]


def test_a_readme_is_admitted_in_a_tier_directory(corpus: Path) -> None:
    """Assert the one exempt name is genuinely exempt

    Paired with the case above so the exemption is pinned from both sides; the
    committed snapshot tier is tracked by exactly such a file.
    """
    (corpus / "snapshots" / "README.md").write_text(
        "# Snapshot tier\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == []


@pytest.mark.parametrize(
    ("field", "placeholder"),
    [("source_url", "n/a"), ("capture_date", "soon")],
)
def test_a_placeholder_provenance_value_is_reported(
    corpus: Path, field: str, placeholder: str
) -> None:
    """Assert a present, non-empty, meaningless value still fails

    "Present and non-empty" is satisfied by ``"n/a"``. The two fields with a
    checkable shape carry a pattern for exactly that reason.
    """
    data = {**CONFORMING_SNAPSHOT_SIDECAR, field: placeholder}
    _rewrite_sidecar(_sidecar_path(corpus, "snapshots", "snapshot"), data)

    assert check_corpus(corpus) == [
        Violation("malformed_field", "snapshots/snapshot.meta.json", field)
    ]


def test_a_readme_is_swept_for_sanitation(corpus: Path) -> None:
    """Assert the exempt filename is exempt from provenance and nothing else

    The escape this closes is concrete: a contributor who wants a page carrying
    a Markdown ``Authorization:`` heading needs only to name it ``README.md``,
    and before this case the whole sanitation sweep could have been carved out
    of the allowed-filename path with every other case still green.

    One row rather than all ten. That every row is armed is a claim the page and
    sidecar sweeps already own; the claim here is the different one that a file
    admitted **by name** is swept at all, and it needs one row to make it.
    """
    specimen = POLICY["sanitation_patterns"]["authorization_header"]["matches"][0]
    (corpus / "snapshots" / "README.md").write_text(
        f"# Snapshot tier\n\n    {specimen}\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [
        Violation("sanitation", "snapshots/README.md", "authorization_header")
    ]


@pytest.mark.parametrize("tier", sorted(POLICY["tiers"]))
def test_an_oversized_readme_is_reported(corpus: Path, tier: str) -> None:
    """Assert a README takes its tier's byte cap like any other corpus file

    Driven per tier because the caps differ: a single case against the larger
    one would pass unchanged if the allowed filename were given a cap of its
    own, or none.
    """
    cap = POLICY["tiers"][tier]["max_bytes"]
    (corpus / tier / "README.md").write_text("x" * (cap + 1), encoding="utf-8", newline="\n")

    assert check_corpus(corpus) == [
        Violation("over_size_cap", f"{tier}/README.md", f"{cap + 1} > {cap}")
    ]


def test_a_readme_with_crlf_line_endings_is_reported(corpus: Path) -> None:
    """Assert the encoding rules reach the exempt filename too"""
    (corpus / "snapshots" / "README.md").write_bytes(b"# Snapshot tier\r\n")

    assert check_corpus(corpus) == [
        Violation(
            "crlf_line_ending", "snapshots/README.md", "corpus files are LF-only"
        )
    ]


def test_a_readme_that_is_not_utf8_is_reported(corpus: Path) -> None:
    """Assert an undecodable README is named rather than skipped

    The other half of the encoding promise. A README is as published as the
    pages beside it, so a byte nobody can read is as unreviewed there.
    """
    (corpus / "snapshots" / "README.md").write_bytes(b"# Caf\xe9 tier\n")

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("undecodable", "snapshots/README.md")
    ]


@pytest.mark.parametrize("field", sorted(POLICY["field_patterns"]))
def test_a_provenance_value_with_a_trailing_newline_is_reported(
    corpus: Path, field: str
) -> None:
    r"""Assert a stray newline inside a value does not satisfy its pattern

    Both patterns are anchored `^...$`, which under `re.search` is not the same
    as "the whole value": without `re.MULTILINE`, `$` matches immediately before
    a final newline. `_is_filled` cannot catch it either, since `strip()` removes
    the newline. Driven for **both** fields -- `source_url` is not exempt by
    virtue of `\S`, because `\S+` simply stops at the newline and `$` matches
    there.
    """
    data = {**CONFORMING_SNAPSHOT_SIDECAR, field: f"{CONFORMING_SNAPSHOT_SIDECAR[field]}\n"}
    _rewrite_sidecar(_sidecar_path(corpus, "snapshots", "snapshot"), data)

    assert check_corpus(corpus) == [
        Violation("malformed_field", "snapshots/snapshot.meta.json", field)
    ]


@pytest.mark.parametrize(("row_id", "specimen"), SANITATION_MATCH_CASES)
def test_each_sanitation_specimen_fires_inside_a_page_body(
    corpus: Path, row_id: str, specimen: str
) -> None:
    """Assert every sanitation shape is caught inside an HTML page"""
    (corpus / "snapshots" / "snapshot.html").write_text(
        f"<article><p>{specimen}</p></article>\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [
        Violation("sanitation", "snapshots/snapshot.html", row_id)
    ]


@pytest.mark.parametrize(("row_id", "specimen"), SANITATION_MATCH_CASES)
def test_each_sanitation_specimen_fires_inside_a_sidecar(
    corpus: Path, row_id: str, specimen: str
) -> None:
    """Assert the sweep covers sidecars, not only pages

    A sidecar is published exactly as the page beside it is, and ``rationale`` is
    free prose -- the most plausible place in this scheme for a real address to
    arrive.
    """
    data = {**CONFORMING_SNAPSHOT_SIDECAR, "rationale": specimen}
    _rewrite_sidecar(_sidecar_path(corpus, "snapshots", "snapshot"), data)

    assert check_corpus(corpus) == [
        Violation("sanitation", "snapshots/snapshot.meta.json", row_id)
    ]


@pytest.mark.parametrize(("row_id", "specimen"), SANITATION_MISS_CASES)
def test_a_negative_specimen_in_a_page_produces_no_violation(
    corpus: Path, row_id: str, specimen: str
) -> None:
    """Assert ordinary prose that mentions the topic is not reported"""
    (corpus / "snapshots" / "snapshot.html").write_text(
        f"<article><p>{specimen}</p></article>\n", encoding="utf-8", newline="\n"
    )

    assert check_corpus(corpus) == [], f"{row_id!r}'s negative specimen was reported"


def test_a_page_directly_under_the_corpus_root_is_reported(corpus: Path) -> None:
    """Assert a file that missed both tier directories is reported

    Not ignored. A sweep that only looks inside the tier directories cannot tell
    a file nobody classified from a file nobody added.
    """
    _write_page(corpus, "loose", CONFORMING_PAGE, CONFORMING_FRAGMENT_SIDECAR)

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("unknown_tier", "loose.html"),
        ("unknown_tier", "loose.meta.json"),
    ]


def test_a_page_under_an_unrecognised_subdirectory_is_reported(corpus: Path) -> None:
    """Assert a third tier invented by a contributor is reported

    ``drafts/`` would otherwise be a directory in which none of the provenance
    or licensing rules apply.
    """
    _write_page(
        corpus / "drafts", "draft", CONFORMING_PAGE, CONFORMING_FRAGMENT_SIDECAR
    )

    assert [(v.rule, v.path) for v in check_corpus(corpus)] == [
        ("unknown_tier", "drafts/draft.html"),
        ("unknown_tier", "drafts/draft.meta.json"),
    ]
