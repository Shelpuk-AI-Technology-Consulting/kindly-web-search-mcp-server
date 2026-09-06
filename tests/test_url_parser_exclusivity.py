"""Assert that no URL is claimed by two of the five specialized parsers.

:func:`~kindly_web_search_mcp_server.content.resolver.resolve_page_content_markdown`
routes a URL by trying the five parsers **in a fixed order** and taking the
first that does not raise. First acceptance wins, so two parsers accepting the
same URL is not a conflict anyone sees — it is a silent mis-route. The later
handler never runs, no error is raised, and the caller receives content
retrieved by the wrong integration, or a canned failure note from it.

That makes mutual exclusivity the load-bearing property of this whole family:

    No URL may be accepted by more than one parser.

Hypothesis states it over a generated space; examples do not, because the
interesting inputs are precisely the ones no author thinks to write down. The
defect this module was written against is the case in point — a GitHub issue
URL whose owner is spelled ``a`` and whose repository name is all digits was
claimed by the **StackExchange** parser, because
``_derive_site_parameter`` ended with a catch-all accepting every ``.com`` host
and a GitHub owner may legitimately be spelled ``a``, ``q`` or ``questions``.

**The generator is deliberately narrow, and that is a trade with a control.**
Free-form text would essentially never produce a URL any parser accepts, so
:func:`urls` composes hosts and paths from the vocabulary the five parsers
match on. That bias is the author's model of the code, which is exactly the
thing a property test is supposed to not depend on. The control, stated here
rather than cited: with the host gate reverted **and** both pinned examples
below deleted, this property still failed on five of five trials from a cleared
example database, shrinking each time to ``https://github.com/a/1/issues/1``. So
generation finds the defect unaided and the pins are belt-and-braces rather than
the only thing holding the claim. ``TEST_SUITE.md`` §3.1 carries the full
mutation record.

An earlier version of this paragraph cited that control as "V3" in "this step's
requirements". No reader of the repository could follow it: `.gitignore`
excludes `.requirements/`, so the document it pointed at is not in the tree —
the same defect as the fabricated citation described at the bottom of this
module, arrived at a different way. A citation that cannot be resolved from a
clean checkout is worth less than the sentence it replaced.

**Acceptance here means "returned at all", which matches the resolver's routing
decision exactly; it is the *rejection* side that is broader.** The resolver
routes to a parser precisely when that parser returns, so the two acceptance
sets are identical. They part company on failure: the resolver catches only each
parser's *own* error type, so a parser raising, say, a ``ValueError`` does not
fall through — it escapes ``resolve_page_content_markdown`` altogether. This
module counts that as a non-acceptance, which is the broader reading, and the
right one for the claim it owns: that two parsers never both succeed. Whether a
rejection uses the correct type is a separate claim, owned by the per-parser
identifier-preservation and rejection step. Both claims are needed and neither implies the other.

The parsers are pure functions of a string, so no case here reads the clock,
the network or the environment, and none needs a seam. The one filesystem touch
is ``Path(__file__).resolve()`` in the ``sys.path`` line every test module in
this repository opens with, which runs at import and feeds nothing any assertion
depends on.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kindly_web_search_mcp_server.content import resolver
from kindly_web_search_mcp_server.content.arxiv import parse_arxiv_url
from kindly_web_search_mcp_server.content.github_discussions import (
    parse_github_discussion_url,
)
from kindly_web_search_mcp_server.content.github_issues import parse_github_issue_url
from kindly_web_search_mcp_server.content.stackexchange import (
    StackExchangeError,
    parse_stackexchange_url,
)
from kindly_web_search_mcp_server.content.wikipedia import parse_wikipedia_url

#: The five parsers, in the order ``resolve_page_content_markdown`` tries them.
#: Order is not what this module asserts — mutual exclusivity makes order
#: irrelevant, which is the point of proving it — but it is the order a reader
#: needs to interpret a counterexample, since the earlier parser is the one that
#: actually wins.
PARSERS: tuple[tuple[str, Callable[[str], object]], ...] = (
    ("stackexchange", parse_stackexchange_url),
    ("github_issue", parse_github_issue_url),
    ("github_discussion", parse_github_discussion_url),
    ("wikipedia", parse_wikipedia_url),
    ("arxiv", parse_arxiv_url),
)

#: A GitHub issue URL that the StackExchange parser also claimed before the
#: allowlist landed. Pinned with ``@example`` so the regression is exercised on
#: every run rather than only when the generator happens to produce it.
KNOWN_GITHUB_OVERLAP = "https://github.com/a/2048/issues/7"

#: The second overlap family, and the one that made the defect ordinary rather
#: than exotic: **any** owner and **any** repository, because `_ISSUE_RE` permits
#: trailing segments while the StackExchange patterns search the whole path.
#:
#: Pinned rather than left to generation. The trailing tail in :data:`PATHS`
#: makes this family reachable, but only at about 0.06% per example against
#: family A's 3% — roughly one run in four at the configured budget. Today both
#: families die to the same host gate, so nothing rests on it; if someone later
#: narrows the fix to exclude `a`, `q` and `questions` as owners, this is the
#: family that would remain and generation alone would catch it a quarter of the
#: time.
KNOWN_GITHUB_TRAILING_OVERLAP = "https://github.com/microsoft/vscode/issues/5/x/q/999"

#: Hosts every parser family claims, plus two no parser claims. Ordered simplest
#: first: Hypothesis shrinks ``sampled_from`` toward earlier entries, so a
#: counterexample reports ``github.com`` rather than ``en.m.wikipedia.org`` when
#: either would do.
HOSTS = st.sampled_from(
    [
        "github.com",
        "stackoverflow.com",
        "arxiv.org",
        "en.wikipedia.org",
        "superuser.com",
        "example.com",
        "www.github.com",
        "math.stackexchange.com",
        "meta.stackexchange.com",
        "meta.stackoverflow.com",
        "en.m.wikipedia.org",
        "example.invalid",
    ]
)

#: GitHub owner names. ``a``, ``q`` and ``questions`` are here because they are
#: the three spellings the StackExchange path patterns look for; they are real,
#: registrable GitHub owner names and not a contrivance.
OWNERS = st.sampled_from(["a", "q", "questions", "owner", "microsoft"])

#: Repository names. All-digit names are common on GitHub (``2048``) and are the
#: half of the overlap the StackExchange patterns supply their id from.
REPOS = st.sampled_from(["1", "2048", "12345", "repo"])

#: Small identifiers. Kept short so a shrunk counterexample stays readable.
NUMBERS = st.sampled_from(["1", "7", "123", "87654321"])

#: Article titles, including one carrying the dot and underscore Wikipedia's
#: canonical form uses.
#:
#: ``q/12345`` is a title only in the sense that ``_WIKI_PATH_RE`` is
#: ``^/wiki/(.+)$`` — the capture is unconstrained, so a title may contain a
#: slash and therefore a StackExchange marker. Same reasoning as
#: :data:`ARXIV_IDS`: without it, widening Wikipedia's host guard produces a
#: real overlap the generator can never see.
TITLES = st.sampled_from(["Apple_Inc.", "Python", "Talk:Python", "q/12345"])

#: arXiv identifiers in both the new (``2401.12345``) and legacy (``math/0309136``)
#: shapes, plus one that matches neither.
#:
#: ``q/1234567`` and ``a/1234567`` are here because a legacy arXiv id is
#: ``<category>/<7 digits>`` with **no constraint on the category**, so a
#: category spelled ``q`` or ``a`` makes the same substring simultaneously a
#: valid arXiv id and a StackExchange question or answer marker. Without these
#: two entries the generator cannot reach the arXiv half of the space at all,
#: and a widened arXiv host guard survives the property untouched — measured,
#: and the reason this pool is not just three realistic ids.
ARXIV_IDS = st.sampled_from(
    [
        "2401.12345",
        "2401.12345v2",
        "math/0309136",
        "q/1234567",
        "a/1234567",
        "nope",
    ]
)

#: Path vocabulary for the free-form shape, which exists so the generator is not
#: confined to the four templates below. Every token either starts a parser's
#: pattern or is ordinary filler.
SEGMENTS = st.sampled_from(
    [
        "a",
        "q",
        "questions",
        "issues",
        "discussions",
        "wiki",
        "abs",
        "pdf",
        "w",
        # `index.php` is here so Wikipedia's *second* acceptance shape is
        # reachable at all: `parse_wikipedia_url` reads `?title=` only when the
        # path is exactly `/w/index.php`. Without it, `w` in this list and
        # `?title=Python` in QUERIES were both inert — measured, zero of 20,000
        # generated URLs could reach that branch.
        "index.php",
        "1",
        "x",
    ]
)

#: Path shapes. The four templates mirror what each parser family matches, and
#: the fifth draws free segments so a shape nobody anticipated can still appear.
#: Crossing these against every host in :data:`HOSTS` — rather than pairing each
#: template with its own host — is the whole point: an overlap lives exactly
#: where one family's path arrives on another family's host.
PATHS = st.one_of(
    st.builds(
        # The tail matters and is not decoration. ``_ISSUE_RE`` is a `match`
        # anchored only at the start, so trailing segments are permitted, while
        # the StackExchange patterns `search` the whole path. That combination
        # made **any** owner and **any** repository overlap before the host fix
        # — `/microsoft/vscode/issues/5/x/q/1` was claimed by both — and a
        # template without a tail can only ever reach the far narrower family
        # where the owner itself is spelled `a`, `q` or `questions`.
        lambda owner, repo, kind, number, tail: (
            f"/{owner}/{repo}/{kind}/{number}"
            + ("/" + "/".join(tail) if tail else "")
        ),
        OWNERS,
        REPOS,
        st.sampled_from(["issues", "discussions"]),
        NUMBERS,
        st.lists(SEGMENTS, max_size=3),
    ),
    st.builds(
        lambda marker, number: f"/{marker}/{number}",
        st.sampled_from(["questions", "q", "a"]),
        NUMBERS,
    ),
    st.builds(lambda title: f"/wiki/{title}", TITLES),
    st.builds(
        lambda prefix, identifier: f"/{prefix}/{identifier}",
        st.sampled_from(["abs", "pdf"]),
        ARXIV_IDS,
    ),
    st.builds(
        lambda segments: "/" + "/".join(segments),
        st.lists(SEGMENTS, max_size=4),
    ),
)

#: Query strings, including the one ``parse_wikipedia_url`` reads a title out of.
QUERIES = st.sampled_from(["", "?title=Python", "?ref=1", "?title="])

#: Fragments, which every parser is supposed to ignore.
FRAGMENTS = st.sampled_from(["", "#top"])


@st.composite
def urls(draw: st.DrawFn) -> str:
    """Generate a URL from the vocabulary the five parsers match on.

    Host and path are drawn independently so that any path shape can arrive on
    any host. That cross product is where an overlap can exist at all — a
    generator pairing each path template with only its own family's host could
    never produce one.

    Args:
        draw: Hypothesis's draw function, supplied by ``@st.composite``.

    Returns:
        An absolute ``https`` URL as a string.
    """
    return f"https://{draw(HOSTS)}{draw(PATHS)}{draw(QUERIES)}{draw(FRAGMENTS)}"


def _accepting_parsers(url: str) -> list[str]:
    """Return the names of every parser that accepts ``url``.

    Acceptance is "did not raise", which is the criterion
    ``resolve_page_content_markdown`` itself applies when it routes.

    Args:
        url: The URL to offer to each of the five parsers.

    Returns:
        The names of the accepting parsers, in resolver order.
    """
    accepting: list[str] = []
    # Deliberately broad: any exception at all is a non-acceptance for routing
    # purposes. Which type a parser raises is another module's claim.
    for name, parse in PARSERS:
        try:
            parse(url)
        except Exception:
            continue
        accepting.append(name)
    return accepting


# 500 rather than the default 100, for margin rather than because 100 was ever
# observed to be flaky. Measured on the unfixed code: family A overlaps at about
# 2-3% per example, and at `max_examples=100` detection was 60/60 across sixty
# cleared-database trials -- Hypothesis does not draw `sampled_from` i.i.d., it
# deliberately diversifies, so the naive binomial estimate of "one miss in eight"
# is simply wrong and an earlier version of this comment said so in error. 500 is
# insurance against a future generator change diluting the space, not a fix for
# an observed flake. Parsing is microseconds, so 500 costs
# nothing. `deadline=None` for a measured reason rather than a feared one: with
# the default 200 ms deadline restored the module still passes, so it is not
# guarding against a spurious `DeadlineExceeded` — it is worth 3-5x wall clock
# (2.6s against 8.7-13.2s measured), because the deadline is checked per example
# and 500 of them pay the cost. Hypothesis's own `ci` profile already sets
# `deadline=None`, so this only changes the local edit-test loop.
# Marked `slow` because §10.5 defines that marker as "over a second" and 500
# examples take about 2.6s. No job in §10.3 deselects `slow`, so this changes no
# selection anywhere -- it keeps the marker table's own definition true of the
# suite, which this repository machine-checks.
@pytest.mark.slow
@settings(max_examples=500, deadline=None)
@given(url=urls())
@example(url=KNOWN_GITHUB_OVERLAP)
@example(url=KNOWN_GITHUB_TRAILING_OVERLAP)
def test_no_url_is_accepted_by_more_than_one_parser(url: str) -> None:
    """Assert at most one of the five parsers accepts any generated URL.

    Args:
        url: A generated URL.
    """
    accepting = _accepting_parsers(url)

    assert len(accepting) <= 1, (
        f"{url!r} was accepted by {len(accepting)} parsers: {', '.join(accepting)}.\n"
        f"resolve_page_content_markdown takes the first acceptance, so "
        f"{accepting[0]!r} wins and {', '.join(accepting[1:])} never runs. "
        f"Nothing reports an error, so this is a silent mis-route."
    )


def test_every_parser_the_resolver_imports_is_covered_here() -> None:
    """Assert this module covers exactly the parsers the resolver module holds.

    Without this, adding a sixth parser would leave the exclusivity property
    quietly covering five of six — passing, and no longer proving what its name
    says. A property with a blind spot is worse than no property, because it
    reports green over the gap.

    **This measures what the resolver module *imports*, not what its body
    actually calls**, and the name says so. The two can differ: a parser
    imported and never dispatched would be demanded here, and — the direction
    that would actually hurt — a parser dispatched through an alias this filter
    does not match would not be. Proving real dispatch means parsing the
    resolver's body, which is a different and more brittle claim; resolver
    routing has its own owner in the risk matrix. Import is a deliberate proxy,
    and it catches the realistic mistake, which is adding a sixth parser to the
    chain and forgetting this module exists.
    """
    dispatched = {
        name
        for name in vars(resolver)
        if name.startswith("parse_") and name.endswith("_url")
    }

    covered = {parse.__name__ for _, parse in PARSERS}

    assert dispatched == covered, (
        f"resolve_page_content_markdown dispatches {sorted(dispatched)} but this "
        f"module covers {sorted(covered)}. Every parser the resolver tries must "
        f"be in PARSERS, or the exclusivity property has a blind spot."
    )


@pytest.mark.parametrize(
    ("url", "expected_site"),
    [
        ("https://stackoverflow.com/questions/123/x", "stackoverflow"),
        ("https://superuser.com/questions/123/x", "superuser"),
        ("https://serverfault.com/questions/123/x", "serverfault"),
        ("https://askubuntu.com/questions/123/x", "askubuntu"),
        ("https://stackapps.com/questions/123/x", "stackapps"),
        ("https://math.stackexchange.com/questions/123/x", "math"),
        ("https://meta.stackexchange.com/questions/123/x", "meta"),
        ("https://meta.stackoverflow.com/questions/123/x", "meta.stackoverflow"),
        ("https://meta.superuser.com/questions/123/x", "meta.superuser"),
        ("https://pt.stackoverflow.com/questions/123/x", "pt.stackoverflow"),
    ],
)
def test_accepts_every_stackexchange_com_network_host_shape(
    url: str, expected_site: str
) -> None:
    """Assert the host allowlist did not narrow away a real ``.com`` network host.

    Scoped to ``.com`` in the name on purpose: ``mathoverflow.net`` is also a
    network host and is deliberately *rejected*, which a name promising "every
    network host" would appear to contradict. That case has its own test.

    The five apex domains, a ``*.stackexchange.com`` community, the
    ``meta.stackexchange.com`` special case, two ``meta.`` subdomains and a
    language subdomain. Every row asserts the derived ``site`` slug, not merely
    that parsing succeeded: the slug is what reaches the Stack Exchange API, so
    a gate that admits the host but derives the wrong slug is still broken.

    Args:
        url: A URL on a Stack Exchange network host.
        expected_site: The ``site`` slug the API expects for that host.
    """
    assert parse_stackexchange_url(url).site == expected_site


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/2048/issues/7",
        "https://www.github.com/a/2048/issues/7",
        "https://example.com/q/12345",
        "https://notstackoverflow.com/questions/1/x",
        "https://xsuperuser.com/questions/1/x",
        "https://meta.example.com/q/12345",
    ],
)
def test_rejects_a_com_host_that_is_not_on_the_stackexchange_network(url: str) -> None:
    """Assert a non-network ``.com`` host is refused, not given a made-up slug.

    ``example.com`` is here alongside the two GitHub hosts on purpose. The
    GitHub rows are the ones the exclusivity property also covers; the
    ``example.com`` row is not, because no second parser claims that host. It is
    the same defect — a site slug invented from a domain that has no Stack
    Exchange community — and only this test can see it.

    **The ``meta.example.com`` row pins the second catch-all.** Before the gate,
    ``host.startswith("meta.") and host.endswith(".com")`` was a catch-all in its
    own right and derived the slug ``meta.example``. It is not reachable by the
    exclusivity property either — no other parser claims that host — so gating
    ahead of every branch rather than replacing only the last one is a claim only
    this row owns.

    **The two suffix rows pin the separating dot**, and they were added because a
    mutation deleted it and survived every other test in this module.
    ``_is_stackexchange_network_host`` asks whether the host *is* a network
    domain or ends with ``"." + domain``. Weaken that to a bare
    ``endswith(domain)`` and ``notstackoverflow.com`` — registrable by anyone —
    becomes Stack Exchange community ``notstackoverflow``. The exclusivity
    property cannot see it, because no second parser claims those hosts either,
    so nothing but a row here distinguishes a suffix from a subdomain.

    Args:
        url: A URL on a ``.com`` host outside the Stack Exchange network.
    """
    with pytest.raises(StackExchangeError):
        parse_stackexchange_url(url)


def test_mathoverflow_is_on_the_network_list_but_still_resolves_to_no_site() -> None:
    """Pin both halves of the deliberately inert ``mathoverflow.net`` entry.

    ``mathoverflow.net`` is a genuine Stack Exchange network domain, so it
    belongs in a constant named for the network. It nonetheless changes no
    behaviour, because every slug-derivation branch requires ``.com`` and
    MathOverflow therefore still derives no site.

    That combination makes deleting the entry an **equivalent mutant** — no
    observable behaviour differs — which matters because §3.2 of the test-suite
    design puts these parsers in the mutation-testing scope, and an entry no
    test can observe is a survivor somebody has to triage. Asserting the private
    predicate is the exception that proves the rule about testing through public
    callers: here the public behaviour is identical either way, so the predicate
    is the only place the intent is observable at all.

    Restoring MathOverflow support is a separate change, and this test is what
    will fail when someone makes it.
    """
    from kindly_web_search_mcp_server.content.stackexchange import (
        _is_stackexchange_network_host,
    )

    assert _is_stackexchange_network_host("mathoverflow.net") is True

    with pytest.raises(StackExchangeError):
        parse_stackexchange_url("https://mathoverflow.net/questions/123/x")


def test_second_level_meta_community_derives_the_wrong_slug_today() -> None:
    """Characterise a known-wrong slug so a future fix is visible, not silent.

    ``math.meta.stackexchange.com`` is a real site whose Stack Exchange API
    ``api_site_parameter`` is ``math.meta``. The ``*.stackexchange.com`` branch
    takes only the first label, so it derives ``math`` and a question on the
    meta site is queried against the **main** site instead.

    This is pre-existing, it is not what the mutual-exclusivity step set out to
    fix, and widening the slug derivation is a behaviour change with its own
    blast radius. It is pinned rather than left undescribed so the defect is
    *characterised* instead of invisible: a test asserting today's wrong answer
    states plainly which behaviour ships, and the day someone corrects the
    derivation this test fails and points them at the decision rather than
    letting the change land unnoticed.

    An earlier version of this docstring justified the test by quoting the
    surrounding design document as claiming these branches "derive correct
    slugs". No document says that. The sentence was removed rather than
    softened, because a fabricated citation is worse than no citation — it
    invites the next reader to trust a source that was never checked.
    """
    assert parse_stackexchange_url(
        "https://math.meta.stackexchange.com/questions/123/x"
    ).site == "math"
