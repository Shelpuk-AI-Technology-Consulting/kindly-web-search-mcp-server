"""Assert what each URL parser returns on its own, and how it declines.

:mod:`~kindly_web_search_mcp_server.content.resolver` routes a URL by offering
it to five parsers in a fixed order and taking the first that does not raise.
``tests/test_url_parser_exclusivity.py`` owns the claim that **at most one**
parser accepts. This module owns the two claims that property is structurally
blind to, both handed here by that step:

* **Identifier preservation.** A URL composed from known identifiers returns
  exactly those identifiers — not a neighbouring capture group, not a
  truncation. Exclusivity is satisfied by a parser that accepts and answers
  wrongly.
* **Typed rejection.** A declining parser raises **its own** error class, which
  is the only class the resolver catches. Exclusivity counts any exception as a
  non-acceptance, which is the broader reading and the right one for *that*
  claim; here the type is the claim. A parser raising anything else escapes
  ``resolve_page_content_markdown`` entirely — the URL never reaches the later
  stages, and ``get_content`` renders the exception text into the Markdown it
  hands back to the calling model.

**The typed-rejection claim is scoped to the branches each parser's own guards
reach, and the scope is load-bearing rather than cautious.** Two inputs escape
that scope today, both measured, both characterised below rather than repaired:
a malformed URL raises out of ``urlsplit`` before any guard runs, in **all
five** parsers, and an over-long StackExchange id raises out of ``int``.
``TEST_SUITE.md`` §14 records the class, and is its only owner. Stating the
domain is what keeps the twenty-one rows below from reading as a universal that a
28-character input falsifies.

**What this module deliberately does not own.** The five per-parser modules
(``tests/test_arxiv.py`` and its siblings) hold the documented real-world URL
shapes each integration advertises. **All five** already assert identifier
preservation for their own parser, and between them they reach five of the
twenty-one rejection branches — so **eleven** of the mutations this module kills
are killed there as well. Counting `tests/test_url_parser_exclusivity.py` as
pre-existing too, which it is, the figure is **thirteen**. Both numbers are
measured, and both are stated because the sentence is otherwise ambiguous about
which set "there" means. ``TEST_SUITE.md`` §3.1 attributes each kill to the
module that produced it rather than letting this one claim them all. Those files
are **not** edited from here: they belong to the pytest-migration batches, whose
verify clause is "only the listed files change".

**Every exception assertion compares the exact class, never ``isinstance``.**
All five error classes subclass :class:`RuntimeError` and nothing else, so they
are siblings: an ``isinstance`` assertion against one would pass on any of the
five, and ``pytest.raises`` alone is an ``isinstance`` check. That is precisely
the mis-typing this module exists to catch, so every ``pytest.raises`` here is
followed by ``assert type(caught.value) is …``.

**Every row of the twenty-one-branch table also asserts a fragment of the
message its branch produces, and that is what makes the row count falsifiable.**
Each parser has between three and seven ``raise`` sites, and a table of rejection
URLs that all happened to trip the *host* guard would look exactly like coverage
of all of them. The fragment names which branch the row reached. It is a mild
coupling to a literal in the source, taken knowingly. No fragment quotes anything
but static English and the hostname already present in the URL under test.

**The other eighty-four rejection rows assert the class only, and inherit their
branch attribution rather than restating it.** The eighty surface rows vary a URL
whose branch the table above already pinned, and the four suffix rows are about
*which host* is refused rather than which branch refuses it. Scoped here because
the paragraph above, written as "every rejection row", was an overclaim inside
this module's own scope — and a reader auditing which claims are self-asserting
cannot tell the two kinds apart from a sentence that covers both.

The parsers are pure functions of a string, so nothing here reads the clock, the
network or the environment — with one deliberate exception, the interpreter's
integer-string conversion ceiling, which **two** fixtures manage and restore.
**Twelve rows** depend on it, in **both** directions, and each takes the fixture
for its direction. Measured by stripping the fixtures and running both ambients:

* **Eleven** need the documented default restored, and fail under
  ``PYTHONINTMAXSTRDIGITS=0`` — the two unconvertible-id rows of the branch
  table, the **eight** surface rows that vary those two branches, and the
  oversized-identifier escape case.
* **One** needs the ceiling *removed*, and fails under the default ambient CI
  actually runs.

Counted in rows, because every other figure in this module is — "149 cases",
"the other eighty-four rejection rows". Two earlier drafts said "three", which
counts *test functions* and silently drops the eight surface rows; both the
default-ambient direction and the row granularity had to be measured rather than
reasoned about, and each was wrong the first time. A case that merely read the
setting would pass or fail on an environment variable nobody in the pull request
chose.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.content import resolver
from kindly_web_search_mcp_server.content.arxiv import ArxivError, parse_arxiv_url
from kindly_web_search_mcp_server.content.github_discussions import (
    GitHubDiscussionError,
    GitHubDiscussionTarget,
    parse_github_discussion_url,
)
from kindly_web_search_mcp_server.content.github_issues import (
    GitHubIssueError,
    GitHubIssueTarget,
    parse_github_issue_url,
)
from kindly_web_search_mcp_server.content.stackexchange import (
    StackExchangeError,
    StackExchangeTarget,
    parse_stackexchange_url,
)
from kindly_web_search_mcp_server.content.wikipedia import (
    WikipediaError,
    WikipediaTarget,
    parse_wikipedia_url,
)


@dataclass(frozen=True)
class ParserCase:
    """One parser, with a URL it accepts and the answer that URL must produce.

    Attributes:
        name: The parser's short name, used as the parametrisation id.
        parse: The parser callable itself.
        error: The error class this parser raises, and the only one the resolver
            catches on its behalf.
        accepted_url: A URL built from the identifiers in ``expected``.
        expected: The exact value ``parse(accepted_url)`` must return.
    """

    name: str
    parse: Callable[[str], Any]
    error: type[Exception]
    accepted_url: str
    expected: Any


#: One row per parser, in the order the resolver tries them.
#:
#: **Every identifier component is pairwise distinct within its row**, which is
#: what makes a shifted capture-group index observable. A GitHub row built from
#: ``("x", "x", 1)`` returns the same tuple whichever way the groups are read;
#: ``octocat`` / ``hello-world`` / ``42`` cannot be permuted into itself.
#:
#: Two rows are shaped by a specific mutation rather than by realism:
#:
#: * The StackExchange row uses ``es.stackoverflow.com`` so the derived slug is
#:   ``es.stackoverflow``, two labels rather than one. The branch it reaches is
#:   the **apex ``.com`` fall-through**, not the ``*.stackexchange.com`` one —
#:   measured, and worth stating because the obvious reading is the other way
#:   round. Narrowing that fall-through to a first label makes the slug ``es``
#:   and fails this row; on ``stackoverflow.com`` the slug is one label already
#:   and the same mutation would return the same answer.
#:
#:   The sibling mutation — narrowing the ``*.stackexchange.com`` branch —
#:   **survives this module**, because the only host with a two-label prefix
#:   there is ``math.meta.stackexchange.com``, whose slug is a *characterised
#:   defect* owned by ``tests/test_url_parser_exclusivity.py``. A row here would
#:   duplicate that module's claim and break when the defect is fixed.
#:   ``TEST_SUITE.md`` §3.1 records the survivor and its owner.
#: * The arXiv row uses the legacy ``hep-th/9901001`` shape. The identifier is
#:   assembled from ``parts[1:]``, so only a multi-segment identifier can tell
#:   that from ``parts[1:2]``. ``tests/test_arxiv.py`` already uses one, so this
#:   row is a second owner of that mutation rather than its only one — measured,
#:   and recorded that way in ``TEST_SUITE.md`` §3.1 rather than claimed here.
PARSER_CASES: tuple[ParserCase, ...] = (
    ParserCase(
        name="stackexchange",
        parse=parse_stackexchange_url,
        error=StackExchangeError,
        accepted_url="https://es.stackoverflow.com/questions/12345/titulo",
        expected=StackExchangeTarget(
            site="es.stackoverflow", question_id=12345, answer_id=None
        ),
    ),
    ParserCase(
        name="github_issue",
        parse=parse_github_issue_url,
        error=GitHubIssueError,
        accepted_url="https://github.com/octocat/hello-world/issues/42",
        expected=GitHubIssueTarget(owner="octocat", repo="hello-world", number=42),
    ),
    ParserCase(
        name="github_discussion",
        parse=parse_github_discussion_url,
        error=GitHubDiscussionError,
        accepted_url="https://github.com/octocat/hello-world/discussions/42",
        expected=GitHubDiscussionTarget(
            owner="octocat", repo="hello-world", number=42
        ),
    ),
    ParserCase(
        name="wikipedia",
        parse=parse_wikipedia_url,
        error=WikipediaError,
        accepted_url="https://es.wikipedia.org/wiki/Manzana",
        expected=WikipediaTarget(
            api_base_url="https://es.wikipedia.org/w/api.php",
            canonical_url="https://es.wikipedia.org/wiki/Manzana",
            host="es.wikipedia.org",
            title="Manzana",
        ),
    ),
    ParserCase(
        name="arxiv",
        parse=parse_arxiv_url,
        error=ArxivError,
        accepted_url="https://arxiv.org/abs/hep-th/9901001",
        expected="hep-th/9901001",
    ),
)


@pytest.mark.parametrize("case", PARSER_CASES, ids=lambda c: c.name)
def test_a_url_built_from_known_identifiers_returns_exactly_those_identifiers(
    case: ParserCase,
) -> None:
    """Assert the parser returns the identifiers its URL was composed from.

    Whole-value equality rather than field-by-field assertions: the parsers
    return frozen dataclasses, so one ``==`` covers every field and a field
    added later cannot slip past this assertion. It still has to be given an
    expected value in ``PARSER_CASES``, which is the point — the edit is
    unavoidable rather than optional.

    Args:
        case: The parser under test and the answer its URL must produce.
    """
    assert case.parse(case.accepted_url) == case.expected


#: Every ``raise`` site reachable in the five parse functions, one row each —
#: three StackExchange, three GitHub Issues, three GitHub Discussions, five
#: Wikipedia, seven arXiv.
#:
#: The rows are the behavioural half of "never a foreign class": a mutation that
#: changes any single ``raise`` in any of the five functions fails exactly one
#: row. A single rejection URL per parser could not do that — it would leave
#: between two and six sites in each parser unexercised while reading as full
#: coverage.
REJECTION_CASES: tuple[
    tuple[str, Callable[[str], Any], type[Exception], str, str], ...
] = (
    # StackExchange — three sites.
    (
        "se_no_host",
        parse_stackexchange_url,
        StackExchangeError,
        "/questions/12345",
        "no hostname",
    ),
    (
        "se_off_network_host",
        parse_stackexchange_url,
        StackExchangeError,
        "https://example.org/questions/12345",
        "Unsupported StackExchange host",
    ),
    (
        "se_no_question_or_answer",
        parse_stackexchange_url,
        StackExchangeError,
        "https://stackoverflow.com/users/1/jon-skeet",
        "not a recognized StackExchange",
    ),
    # GitHub Issues — three sites.
    (
        "issue_foreign_host",
        parse_github_issue_url,
        GitHubIssueError,
        "https://example.org/octocat/hello-world/issues/42",
        "Unsupported GitHub host",
    ),
    (
        "issue_wrong_path",
        parse_github_issue_url,
        GitHubIssueError,
        "https://github.com/octocat/hello-world/pull/42",
        "not a recognized GitHub Issue",
    ),
    # The third site is the conversion guard, and it is reachable for the same
    # reason the StackExchange one is: CPython refuses to build an int from more
    # than 4300 decimal digits. GitHub's parsers already wrap the conversion,
    # which is what makes this row a *rejection* here and an escape there.
    (
        "issue_unconvertible_number",
        parse_github_issue_url,
        GitHubIssueError,
        "https://github.com/octocat/hello-world/issues/" + "1" * 5000,
        "Invalid issue number",
    ),
    # GitHub Discussions — the same three.
    (
        "discussion_foreign_host",
        parse_github_discussion_url,
        GitHubDiscussionError,
        "https://example.org/octocat/hello-world/discussions/42",
        "Unsupported GitHub host",
    ),
    (
        "discussion_wrong_path",
        parse_github_discussion_url,
        GitHubDiscussionError,
        "https://github.com/octocat/hello-world/issues/42",
        "not a recognized GitHub Discussion",
    ),
    (
        "discussion_unconvertible_number",
        parse_github_discussion_url,
        GitHubDiscussionError,
        "https://github.com/octocat/hello-world/discussions/" + "1" * 5000,
        "Invalid discussion number",
    ),
    # Wikipedia — five sites.
    (
        "wiki_no_host",
        parse_wikipedia_url,
        WikipediaError,
        "/wiki/Manzana",
        "no hostname",
    ),
    (
        "wiki_foreign_host",
        parse_wikipedia_url,
        WikipediaError,
        "https://example.org/wiki/Manzana",
        "Unsupported Wikipedia host",
    ),
    (
        "wiki_wrong_path",
        parse_wikipedia_url,
        WikipediaError,
        "https://es.wikipedia.org/notwiki/Manzana",
        "not a recognized Wikipedia article",
    ),
    # `%09` is a tab: it survives `unquote`, is not a space so the underscore
    # substitution leaves it alone, and `.strip()` then empties it. Any non-space
    # whitespace does the same -- `%0A`, `%0B`, `%0C`. A space does not: `%20`
    # becomes an underscore, which `.strip()` keeps. `_WIKI_PATH_RE` requires at
    # least one character after `/wiki/`, so an empty capture cannot get here.
    (
        "wiki_empty_title",
        parse_wikipedia_url,
        WikipediaError,
        "https://es.wikipedia.org/wiki/%09",
        "Empty article title",
    ),
    (
        "wiki_non_article_namespace",
        parse_wikipedia_url,
        WikipediaError,
        "https://es.wikipedia.org/wiki/Talk:Manzana",
        "Non-article Wikipedia namespace",
    ),
    # arXiv — seven sites.
    ("arxiv_no_host", parse_arxiv_url, ArxivError, "/abs/2401.12345", "no hostname"),
    (
        "arxiv_foreign_host",
        parse_arxiv_url,
        ArxivError,
        "https://example.org/abs/2401.12345",
        "Unsupported arXiv host",
    ),
    ("arxiv_no_path", parse_arxiv_url, ArxivError, "https://arxiv.org", "no path"),
    (
        "arxiv_single_segment",
        parse_arxiv_url,
        ArxivError,
        "https://arxiv.org/abs",
        "not a recognized arXiv paper",
    ),
    (
        "arxiv_wrong_prefix",
        parse_arxiv_url,
        ArxivError,
        "https://arxiv.org/other/2401.12345",
        "not a recognized arXiv abs/pdf",
    ),
    (
        "arxiv_empty_identifier",
        parse_arxiv_url,
        ArxivError,
        "https://arxiv.org/pdf/.pdf",
        "Empty arXiv identifier",
    ),
    (
        "arxiv_unrecognized_identifier",
        parse_arxiv_url,
        ArxivError,
        "https://arxiv.org/abs/nope",
        "Unrecognized arXiv identifier format",
    ),
)


@pytest.mark.parametrize(
    ("label", "parse", "error", "url", "fragment"),
    REJECTION_CASES,
    ids=[row[0] for row in REJECTION_CASES],
)
def test_every_rejection_branch_raises_its_own_error_class(
    default_int_digits: None,
    label: str,
    parse: Callable[[str], Any],
    error: type[Exception],
    url: str,
    fragment: str,
) -> None:
    """Assert one ``raise`` site produces the parser's own class, and only it.

    **The whole table takes the conversion-ceiling fixture, not just the two rows
    that need it.** ``issue_unconvertible_number`` and
    ``discussion_unconvertible_number`` are the only path to those parsers'
    third ``raise`` site, and they reach it only because CPython's default
    ``int_max_str_digits`` is 4300. Measured: under ``PYTHONINTMAXSTRDIGITS=0``
    and without the fixture, both rows fail with ``DID NOT RAISE`` — so the
    twenty-one-row claim silently lost two rows on an environment nobody in the
    pull request chose. Applying the fixture per row would need a second
    parametrisation axis to carry it; applying it to the table costs one
    ``sys.set_int_max_str_digits`` call per row and makes every row's meaning
    independent of the ambient interpreter.

    Args:
        default_int_digits: Fixture pinning the interpreter's integer-string
            conversion ceiling to its documented default.
        label: The row's parametrisation id, naming the branch.
        parse: The parser callable.
        error: The class the resolver catches for this parser.
        url: A URL that reaches exactly the branch ``label`` names.
        fragment: A substring of that branch's message, which is what
            distinguishes this row from every other row on the same parser.
    """
    with pytest.raises(error) as caught:
        parse(url)

    assert type(caught.value) is error
    assert fragment in str(caught.value), (
        f"{label} was meant to reach the branch whose message contains "
        f"{fragment!r}, but the parser raised {str(caught.value)!r}. The row is "
        f"exercising a different branch than it claims, so the site it names is "
        f"untested."
    )


def test_the_error_classes_asserted_here_are_the_ones_the_resolver_imports() -> None:
    """Assert this module's error classes are the set the resolver routes on.

    Without this, adding a sixth parser, or renaming an error class, would leave
    every row above green while the resolver stopped catching what they assert.

    **Imports, not the resolver's body — and that is a decision taken from the
    sibling module rather than freshly.**
    ``tests/test_url_parser_exclusivity.py`` records that proving real dispatch
    means parsing the resolver's body, "a different and more brittle claim",
    and declines it for the same reason. Re-opening that here would give one
    question two answers. Import is a deliberate proxy and it catches the
    realistic mistake, which is a sixth parser arriving with nobody editing
    this module.
    """
    imported = {
        name for name in vars(resolver) if name.endswith("Error")
    }

    asserted = {case.error.__name__ for case in PARSER_CASES}

    assert imported == asserted, (
        f"resolver.py routes on {sorted(imported)}, but this module asserts "
        f"{sorted(asserted)}. A parser raising a class the resolver does not "
        f"catch escapes resolve_page_content_markdown entirely."
    )


#: A host that merely *ends with* a parser's accepted domain — every one of them
#: registrable by anyone.
#:
#: The mutual-exclusivity step fixed and pinned exactly this class on the
#: StackExchange side, and cannot reach the other four: a guard widened into
#: host space **no second parser claims** produces no overlap, so nothing
#: accepts a URL twice and the property has nothing to see. Measured before this
#: module existed — all three of these hosts, and the arXiv one below, passed the
#: entire gate selection.
#:
#: arXiv is the row that needed no mutation. ``parse_arxiv_url`` matched
#: ``endswith("arxiv.org")`` with no leading dot, so ``notarxiv.org`` was
#: accepted on ``main`` — a live defect, repaired with this module rather than
#: a mutant this module kills.
SUFFIX_NOT_SUBDOMAIN_CASES: tuple[
    tuple[str, Callable[[str], Any], type[Exception], str], ...
] = (
    ("arxiv", parse_arxiv_url, ArxivError, "https://notarxiv.org/abs/2401.12345"),
    (
        "wikipedia",
        parse_wikipedia_url,
        WikipediaError,
        "https://notwikipedia.org/wiki/Manzana",
    ),
    (
        "github_issue",
        parse_github_issue_url,
        GitHubIssueError,
        "https://notgithub.com/octocat/hello-world/issues/42",
    ),
    (
        "github_discussion",
        parse_github_discussion_url,
        GitHubDiscussionError,
        "https://notgithub.com/octocat/hello-world/discussions/42",
    ),
)


@pytest.mark.parametrize(
    ("label", "parse", "error", "url"),
    SUFFIX_NOT_SUBDOMAIN_CASES,
    ids=[row[0] for row in SUFFIX_NOT_SUBDOMAIN_CASES],
)
def test_a_host_that_only_ends_with_the_accepted_domain_is_rejected(
    label: str,
    parse: Callable[[str], Any],
    error: type[Exception],
    url: str,
) -> None:
    """Assert a lookalike domain is not claimed by the integration it imitates.

    Args:
        label: The parser's short name.
        parse: The parser callable.
        error: The class it must raise.
        url: A URL on a host that only ends with the accepted domain.
    """
    with pytest.raises(error) as caught:
        parse(url)

    assert type(caught.value) is error


#: The hosts each guard must keep accepting — the control on the test above.
#:
#: Without these rows, narrowing a host guard until it rejects everything would
#: satisfy the suffix cases perfectly. Each parser's rule is written differently
#: and the rows follow the rule rather than a single phrasing: arXiv accepts the
#: apex and its subdomains, Wikipedia accepts subdomains only, and both GitHub
#: parsers accept exactly two hosts. ``export.arxiv.org`` is the row that matters
#: most — the repair had to keep the apex *and* its subdomains, and an equality
#: test would have kept only the apex.
ACCEPTED_HOST_CASES: tuple[tuple[str, Callable[[str], Any], str], ...] = (
    ("arxiv_apex", parse_arxiv_url, "https://arxiv.org/abs/2401.12345"),
    ("arxiv_subdomain", parse_arxiv_url, "https://export.arxiv.org/abs/2401.12345"),
    (
        "wikipedia_subdomain",
        parse_wikipedia_url,
        "https://es.wikipedia.org/wiki/Manzana",
    ),
    (
        "github_apex",
        parse_github_issue_url,
        "https://github.com/octocat/hello-world/issues/42",
    ),
    (
        "github_www",
        parse_github_issue_url,
        "https://www.github.com/octocat/hello-world/issues/42",
    ),
    (
        "github_discussion_www",
        parse_github_discussion_url,
        "https://www.github.com/octocat/hello-world/discussions/42",
    ),
)


@pytest.mark.parametrize(
    ("label", "parse", "url"),
    ACCEPTED_HOST_CASES,
    ids=[row[0] for row in ACCEPTED_HOST_CASES],
)
def test_the_hosts_each_parser_owns_are_still_accepted(
    label: str, parse: Callable[[str], Any], url: str
) -> None:
    """Assert the guard that rejects a lookalike still admits the real thing.

    Args:
        label: The row's parametrisation id.
        parse: The parser callable.
        url: A URL on a host the parser owns.
    """
    parse(url)


def _with_trailing_slash(url: str) -> str:
    """Append a slash to a URL's path.

    Args:
        url: The URL to vary. No URL in this module carries a query string, so
            appending is enough and no splitting is needed.

    Returns:
        The same URL with one more slash at the end of its path.
    """
    return f"{url}/"


def _with_upper_case_scheme(url: str) -> str:
    """Upper-case a URL's scheme.

    Args:
        url: The URL to vary.

    Returns:
        The same URL spelled ``HTTPS://``.

    Raises:
        AssertionError: When ``url`` is not spelled ``https://``, which would
            make this variation a silent no-op and the rows using it vacuous.
    """
    varied = url.replace("https://", "HTTPS://", 1)
    assert varied != url, f"{url!r} is not an https URL, so this varies nothing"
    return varied


def _with_a_query(url: str) -> str:
    """Append two parameters no parser reads.

    Args:
        url: The URL to vary.

    Returns:
        The same URL carrying ``utm_source`` before ``ref``.
    """
    return f"{url}?utm_source=b&ref=a"


def _with_the_query_reversed(url: str) -> str:
    """Append the same two parameters in the opposite order.

    Args:
        url: The URL to vary.

    Returns:
        The same URL carrying ``ref`` before ``utm_source``.
    """
    return f"{url}?ref=a&utm_source=b"


#: The surface variations §3.1 names, as functions of a URL. Each is a shape a
#: clipboard produces without anyone meaning anything by it.
#:
#: **Query order is two variations, not one, and that is what makes the claim
#: falsifiable.** A single appended query string tests only that a query is
#: ignored; order-independence needs both orderings compared against the same
#: expected answer.
#:
#: **The scheme variation has no killing mutation, by construction.** No parser
#: reads ``parsed.scheme``, and ``urlsplit`` lowercases ``hostname`` regardless
#: of how the scheme is spelled. It is regression armour, and it is named as such
#: in ``TEST_SUITE.md`` §3.1 so a mutation report is not read as coverage.
SURFACE_VARIATIONS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("trailing_slash", _with_trailing_slash),
    ("upper_case_scheme", _with_upper_case_scheme),
    ("query", _with_a_query),
    ("query_reversed", _with_the_query_reversed),
)


#: Every (rejection branch, variation) pair, minus the pairs that cannot exist
#: and the one that does not hold.
#:
#: **This runs over all twenty-one branches, not over one rejected URL per
#: parser, and the difference is not cosmetic.** An earlier version varied five
#: URLs — one per parser — and §3.1's claim was written as though it covered the
#: branches. Eighty-one pairs exist and exactly one fails; varying five of them
#: could not see it, and the automated review is what found the gap.
#:
#: Two exclusions, and they are different in kind:
#:
#: * The three relative-URL rows carry no scheme, so there is nothing for the
#:   scheme variation to vary. Arithmetic, not an exemption — 21 x 4 - 3 = 81.
#: * ``("wiki_empty_title", "trailing_slash")`` **flips from rejection to
#:   acceptance**, and is excluded by name with
#:   :func:`test_a_trailing_slash_turns_the_empty_wikipedia_title_into_a_slash`
#:   pinning what happens instead.
REJECTION_SURFACE_ROWS: tuple[
    tuple[str, Callable[[str], Any], type[Exception], str, str, Callable[[str], str]],
    ...
] = tuple(
    (label, parse, error, url, variation_name, vary)
    for label, parse, error, url, _fragment in REJECTION_CASES
    for variation_name, vary in SURFACE_VARIATIONS
    if not (variation_name == "upper_case_scheme" and not url.startswith("https://"))
    and (label, variation_name) != ("wiki_empty_title", "trailing_slash")
)


@pytest.mark.parametrize(
    ("label", "parse", "error", "url", "variation_name", "vary"),
    REJECTION_SURFACE_ROWS,
    ids=[f"{label}-{name}" for label, _, _, _, name, _ in REJECTION_SURFACE_ROWS],
)
def test_rejection_does_not_depend_on_the_surface_of_the_url(
    default_int_digits: None,
    label: str,
    parse: Callable[[str], Any],
    error: type[Exception],
    url: str,
    variation_name: str,
    vary: Callable[[str], str],
) -> None:
    """Assert a declined URL is declined the same way after a cosmetic change.

    This is the claim §3.1 states — *rejection* is stable — and it is asserted
    over every branch the claim quantifies over rather than over one URL per
    parser. Acceptance stability is asserted separately below, because it holds
    for fewer pairs and saying so is the honest shape.

    Args:
        default_int_digits: Fixture pinning the interpreter's integer-string
            conversion ceiling. **Eight** of this grid's rows need it — the two
            unconvertible-id branches crossed against all four variations — not
            the two the branch table needs, which is what an earlier draft of
            this line said.
        label: The rejection branch's parametrisation id.
        parse: The parser callable.
        error: The class the resolver catches for this parser.
        url: The branch's URL, before the variation.
        variation_name: The variation's parametrisation id.
        vary: The transform applied to the URL.
    """
    with pytest.raises(error) as caught:
        parse(vary(url))

    assert type(caught.value) is error


def test_a_trailing_slash_turns_the_empty_wikipedia_title_into_a_slash() -> None:
    """Characterise the one rejection that a cosmetic change turns into a claim.

    ``https://es.wikipedia.org/wiki/%09`` is declined — the tab survives
    ``unquote``, is not a space so the underscore substitution leaves it, and
    ``.strip()`` empties it. Append a slash and the greedy capture takes
    ``%09/``, which unquotes to ``"\t/"`` and strips to ``"/"`` — not empty, not
    a namespace prefix. So the parser **returns**, and the URL is claimed by the
    Wikipedia integration instead of declining to the universal loader.

    Same root cause as
    :func:`test_a_trailing_slash_changes_the_wikipedia_title_today` — a greedy
    ``_WIKI_PATH_RE`` capture normalised too late — and the same treatment:
    characterised, not repaired, and recorded in ``TEST_SUITE.md`` §14, which is
    where the decision lives. This one is the worse half of the pair,
    because it changes *whether* the parser accepts rather than only *what* it
    returns, and it is the only one of eighty-one (branch, variation) pairs that
    does not hold.

    Found by the automated review on the pull request, after a version of this
    module that varied one rejected URL per parser and a §3.1 sentence that read
    as though it covered all twenty-one branches.
    """
    target = parse_wikipedia_url("https://es.wikipedia.org/wiki/%09/")

    assert target.title == "/"
    assert target.canonical_url == "https://es.wikipedia.org/wiki//"


#: Every (parser, variation) pair whose *acceptance* is invariant — all of them
#: but one.
#:
#: ``("wikipedia", "trailing_slash")`` is excluded because the answer genuinely
#: changes there, and that is a finding rather than an exemption:
#: :func:`test_a_trailing_slash_changes_the_wikipedia_title_today` pins what
#: happens instead. Excluding the pair rather than the parser keeps Wikipedia
#: under the other three variations, where the claim does hold.
ACCEPTANCE_SURFACE_ROWS: tuple[
    tuple[ParserCase, str, Callable[[str], str]], ...
] = tuple(
    (case, variation_name, vary)
    for case in PARSER_CASES
    for variation_name, vary in SURFACE_VARIATIONS
    if (case.name, variation_name) != ("wikipedia", "trailing_slash")
)


@pytest.mark.parametrize(
    ("case", "variation_name", "vary"),
    ACCEPTANCE_SURFACE_ROWS,
    ids=[f"{case.name}-{name}" for case, name, _ in ACCEPTANCE_SURFACE_ROWS],
)
def test_accepted_identifiers_do_not_depend_on_the_surface_of_the_url(
    case: ParserCase, variation_name: str, vary: Callable[[str], str]
) -> None:
    """Assert a cosmetic change to an accepted URL changes no identifier.

    Args:
        case: The parser under test and a URL it accepts.
        variation_name: The variation's parametrisation id.
        vary: The transform applied to the URL.
    """
    assert case.parse(vary(case.accepted_url)) == case.expected


def test_a_percent_encoded_arxiv_identifier_is_decoded_before_it_is_matched() -> None:
    """Assert the path is percent-decoded, which nothing else in the tree pins.

    A legacy arXiv identifier carries a slash — ``hep-th/9901001`` — and a client
    that percent-encodes path separators sends ``hep-th%2F9901001``. The parser
    calls :func:`~urllib.parse.unquote` before splitting, so the encoded form
    reaches the same identifier as the plain one.

    Added because a mutation found the line unowned: with the ``unquote`` call
    removed, every other case in this module and in all five per-parser modules
    still passed, and the encoded identifier failed the format check instead of
    resolving. It is a one-row claim, and it is the only thing holding that call.

    The neighbouring ``.rstrip("/")`` on the same line is a different matter and
    is **recorded as an equivalent mutant** rather than given a row: the next
    line filters empty segments out of the split, so the trailing slash is
    dropped either way. Measured. ``TEST_SUITE.md`` §3.1 carries it so a mutation
    report has an answer to point at.
    """
    assert parse_arxiv_url("https://arxiv.org/abs/hep-th%2F9901001") == "hep-th/9901001"


def test_a_trailing_slash_changes_the_wikipedia_title_today() -> None:
    """Characterise the one parser whose acceptance is not surface-invariant.

    ``_WIKI_PATH_RE`` is ``^/wiki/(.+)$`` and captures greedily, so a trailing
    slash becomes part of the title. The request that follows asks MediaWiki for
    ``Manzana/``, which is not the article the URL names, and the canonical URL
    handed back to the caller carries the slash too.

    **Characterised, not repaired.** §3.1 scopes this step's stability claim to
    *rejection*; normalising the captured title changes which request the
    Wikipedia integration issues, which is a behaviour change no step has been
    asked for. Pinning today's answer is what makes a future correction visible
    instead of silent — the same treatment ``math.meta.stackexchange.com``
    already receives in ``tests/test_url_parser_exclusivity.py``. §14 records the
    gap and names this test as its characterisation.
    """
    target = parse_wikipedia_url("https://es.wikipedia.org/wiki/Manzana/")

    assert target.title == "Manzana/"
    assert target.canonical_url == "https://es.wikipedia.org/wiki/Manzana/"


#: A URL `urlsplit` refuses to parse: an unmatched bracket reads as the start of
#: an IPv6 literal. Twenty-eight characters, and nothing exotic about it — this
#: is the shape a model produces from a truncated or concatenated link.
MALFORMED_URL = "https://[oops/abs/2401.12345"


@pytest.mark.parametrize(
    "parse",
    [case.parse for case in PARSER_CASES],
    ids=[case.name for case in PARSER_CASES],
)
def test_a_malformed_url_escapes_every_parser_as_a_foreign_class_today(
    parse: Callable[[str], Any],
) -> None:
    """Characterise the escape the typed-rejection claim is scoped around.

    ``parsed.hostname`` is read as the first act of every one of the five parse
    functions, and ``urlsplit`` raises :class:`ValueError` before any guard can
    run. :class:`ValueError` is not any parser's own class, so
    ``resolve_page_content_markdown``'s ``except`` clause does not catch it: the
    URL never reaches the later stages, and ``get_content`` renders the
    exception's text into the Markdown it returns to the calling model.

    **This asserts today's wrong answer on purpose.** The repair is one guard
    per parser, five modules, and this step ships one production edit — the same
    scoping the search-provider error-path step recorded when it found a
    credential leak it was not scoped to fix. ``TEST_SUITE.md`` §14 carries the
    gap, and carries it alone — the tracker issue raised alongside it was
    withdrawn as sub-threshold, so §14 is the record, not a pointer to one. When
    the repair lands, this test fails and points at the decision instead of
    letting the change go unnoticed.

    Args:
        parse: One of the five parser callables.
    """
    with pytest.raises(ValueError) as caught:
        parse(MALFORMED_URL)

    assert type(caught.value) is ValueError


@pytest.fixture
def unbounded_int_digits() -> Iterator[None]:
    """Remove the interpreter's integer-string conversion limit for one test.

    The 4300-digit default is **process-global and settable from outside the
    process** — ``PYTHONINTMAXSTRDIGITS``, ``-X int_max_str_digits`` and
    :func:`sys.set_int_max_str_digits` all move it. A case that merely reads it
    is therefore not deterministic: under ``PYTHONINTMAXSTRDIGITS=0`` the
    conversion below succeeds and the case asserting an exception fails, on an
    environment nobody in the pull request chose.

    So the pair of cases sets the limit rather than reading it: one restores the
    documented default and asserts the escape, the other removes the limit and
    asserts what happens instead. Between them they say the honest thing — the
    parser has **no bound of its own**.

    Yields:
        ``None``; the previous limit is restored on teardown.
    """
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.fixture
def default_int_digits() -> Iterator[None]:
    """Pin the interpreter's integer-string conversion limit to CPython's default.

    See :func:`unbounded_int_digits` for why the limit is set rather than read.

    Yields:
        ``None``; the previous limit is restored on teardown.
    """
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(sys.int_info.default_max_str_digits)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


#: A StackExchange question id of five thousand digits — over CPython's default
#: conversion ceiling, and matched by ``_QUESTION_RE`` like any other run of
#: digits.
OVERSIZED_ID_URL = "https://stackoverflow.com/q/" + "1" * 5000


def test_an_oversized_stackexchange_id_escapes_as_a_foreign_class_today(
    default_int_digits: None,
) -> None:
    """Characterise the second escape: an id ``int`` refuses to build.

    ``parse_stackexchange_url`` converts the matched id with a bare ``int``,
    unlike both GitHub parsers, which wrap theirs. Over the interpreter's
    conversion ceiling that raises :class:`ValueError` — again a class the
    resolver does not catch, so the URL never reaches the later stages.

    Characterised rather than repaired, for the reason
    :func:`test_a_malformed_url_escapes_every_parser_as_a_foreign_class_today`
    gives, and for one more that is specific to this input: see
    :func:`test_the_parser_has_no_bound_of_its_own_on_the_id_length`.

    Args:
        default_int_digits: Fixture pinning the conversion ceiling.
    """
    with pytest.raises(ValueError) as caught:
        parse_stackexchange_url(OVERSIZED_ID_URL)

    assert type(caught.value) is ValueError


def test_the_parser_has_no_bound_of_its_own_on_the_id_length(
    unbounded_int_digits: None,
) -> None:
    """Assert the ceiling above is the interpreter's, not the parser's.

    This is what makes the "wrap the conversion" repair insufficient rather than
    merely out of scope. With the interpreter's limit removed the parser accepts
    the id and would forward five thousand digits to the Stack Exchange API, so
    a ``try``/``except`` around the conversion would guard the *symptom* on
    default-configured interpreters and nothing at all elsewhere.

    Recorded in ``TEST_SUITE.md`` §14, which owns the decision.

    Args:
        unbounded_int_digits: Fixture removing the conversion ceiling.
    """
    target = parse_stackexchange_url(OVERSIZED_ID_URL)

    assert target.site == "stackoverflow"
    assert len(str(target.question_id)) == 5000


def test_a_short_question_link_is_read_as_a_question() -> None:
    """Assert ``/q/<id>`` resolves to a question id, not to nothing.

    Stack Exchange's own share dialog produces this form, so it is the shape a
    user is most likely to paste. ``_QUESTION_RE`` accepts ``questions`` or
    ``q``; nothing pinned the second alternative before this test, and dropping
    it left the whole gate selection green while every shared link stopped
    resolving.
    """
    target = parse_stackexchange_url("https://es.stackoverflow.com/q/12345")

    assert target == StackExchangeTarget(
        site="es.stackoverflow", question_id=12345, answer_id=None
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://stackoverflow.com/beta/questions/12345",
            StackExchangeTarget(
                site="stackoverflow", question_id=12345, answer_id=None
            ),
        ),
        (
            "https://stackoverflow.com/beta/a/54321",
            StackExchangeTarget(
                site="stackoverflow", question_id=None, answer_id=54321
            ),
        ),
    ],
    ids=["question_marker", "answer_marker"],
)
def test_a_marker_below_the_root_of_the_path_is_still_read(
    url: str, expected: StackExchangeTarget
) -> None:
    """Characterise the unanchored search both StackExchange patterns perform.

    **This pins today's behaviour; it does not endorse it, and the URLs are
    synthetic.** ``TEST_SUITE.md`` §3.1 records that the claim these markers
    "appear mid-path on real URLs" had no citation and no URL anyone could
    produce on an allowlisted host — the closest candidate lives on
    ``stackoverflowteams.com``, which the allowlist rejects. Nothing is known to
    need this form.

    The same section keeps anchoring open as a future option, so the value here
    is regression visibility rather than a promise: changing either ``search`` to
    ``match`` narrows what the tool resolves, and before this test it was
    invisible. **A step that anchors these patterns is expected to delete these
    two rows**, not to work around them.

    Args:
        url: A URL whose marker is not the first path segment.
        expected: The target the parser produces for it today.
    """
    assert parse_stackexchange_url(url) == expected
