"""Select which review rule files apply to a pull request's changed files.

Claude Code has no equivalent of GitHub Copilot's ``applyTo`` frontmatter glob,
so this module reproduces it: given the list of files a pull request touches, it
decides which rule files under ``.github/review/rules/`` belong in the review
prompt.

Adopted from an internal repository where this system is in production. The selector
mechanics -- :func:`_matches`, :func:`select`, :func:`resolve_paths` and the
command-line entry point -- are carried across unchanged so a fix made upstream
can be copied here. :data:`RULE_SPECS` is the half that is ours: it is this
repository's component layout, including the fan-out rule that makes a shared
module's change pull in the rules of everything that imports it.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path

RULES_DIR = Path(".github/review/rules")


@dataclass(frozen=True)
class RuleSpec:
    """A review rule file and the paths that activate it.

    Attributes:
        name: Rule file stem, matching a file in :data:`RULES_DIR`.
        patterns: Glob patterns; any match activates the rule.
        pulls_in: Other rule names activated whenever this one is, used to model
            dependency fan-out rather than mere directory membership.
    """

    name: str
    patterns: tuple[str, ...]
    pulls_in: tuple[str, ...] = ()


#: The package root, named once. Every source pattern below is anchored on it, and
#: writing it out eleven times is how a rename leaves half the selector matching
#: nothing while the other half still fires -- a partial selection is worse than
#: none, because the review looks rule-driven and is not.
PKG = "src/kindly_web_search_mcp_server"


# Ordered most general to most specific; ordering controls prompt assembly order.
RULE_SPECS: tuple[RuleSpec, ...] = (
    # `models.py`, `settings.py` and `utils/` are imported by every other module in
    # the package -- the search providers, the content resolvers, the scrapers and
    # the server itself all read configuration through `settings` and return the
    # shared result types. So a change here IS a change to each of them, and
    # scoping strictly by directory would let a settings default or a result-model
    # field flip ship with none of its consumers' rules applied.
    #
    # `utils/diagnostics.py` is the sharpest of the three: it is what masks API
    # keys out of logs and error text, and `tests/test_diagnostics_masking.py`
    # exists because getting that wrong publishes a customer's search key. A
    # change to it needs the security section of every consumer's rule loaded.
    RuleSpec(
        name="core",
        patterns=(
            f"{PKG}/models.py",
            f"{PKG}/settings.py",
            f"{PKG}/utils/**/*.py",
            # The package's own entry points. `__init__.py` decides the public
            # surface and `__main__.py` is what `python -m` runs; neither is
            # reachable by the three patterns above.
            f"{PKG}/__init__.py",
            f"{PKG}/__main__.py",
        ),
        pulls_in=(
            "mcp-server",
            "search-providers",
            "content-resolvers",
            "scrape-browser",
        ),
    ),
    # The MCP surface: tool registration and descriptions, the transport choice
    # (stdio / streamable HTTP / SSE), and the host and origin allowlists that
    # `SECURITY.md` and README § "Host and origin allowlists" describe. `cli.py`
    # is the same component from the other end -- it is what `uvx` runs, and it
    # decides which transport `server.main` is entered with.
    RuleSpec(
        name="mcp-server",
        patterns=(
            f"{PKG}/server.py",
            f"{PKG}/cli.py",
        ),
    ),
    # The five search backends plus the shared SERP base class. They are one rule
    # rather than five because they share a single contract -- normalise a
    # provider's response into the shared result model, read their key from
    # settings, never leak it -- and `tests/test_provider_registry_consistency.py`
    # is the proof that the registry stays in step with them.
    RuleSpec(
        name="search-providers",
        patterns=(f"{PKG}/search/**/*.py",),
    ),
    # The per-site content resolvers (arXiv, GitHub issues and discussions,
    # Stack Exchange, Wikipedia) and the dispatcher that chooses between them.
    # Their shared contract is different from the search providers': each one
    # turns a *known* site's API or markup into Markdown, and the dispatcher's
    # job is deciding when none of them applies and the universal path should
    # run instead.
    RuleSpec(
        name="content-resolvers",
        patterns=(f"{PKG}/content/**/*.py",),
    ),
    # The universal retrieval path: HTTP fetch, HTML extraction and sanitising,
    # and the headless-Chromium fallback. Its own rule rather than folded into
    # the resolvers, because it is the only part of this repository that starts a
    # subprocess and hands it a command line -- which is a different risk class
    # from parsing a JSON API, and the reason
    # `tests/test_worker_launch_args_redaction.py` and
    # `tests/test_nodriver_worker_sandbox.py` exist.
    RuleSpec(
        name="scrape-browser",
        patterns=(f"{PKG}/scrape/**/*.py",),
    ),
    RuleSpec(
        name="python-tests",
        patterns=(
            "tests/**/*.py",
            # The review workflow's own tests are tests too. Omitting them would
            # mean the file guarding this very selector is reviewed without the
            # test rules applied.
            ".github/review/tests/**/*.py",
        ),
    ),
    # How the server is built, installed and configured -- not "bookkeeping
    # files". `pyproject.toml` carries the dependency bounds that keep the server
    # importable at all: `mcp>=1.25,<2` is there because `server.py` imports
    # `mcp.server.fastmcp`, which 2.0.0 removed, and the documented `uvx --from
    # git+https://...` install path re-resolves from PyPI on every start. A bound
    # lifted here breaks every user on their next launch, which is why this rule
    # pulls in `mcp-server` rather than standing alone.
    #
    # `tests/test_dependency_constraints.py` is the guard that already exists for
    # this file; a change to the bounds without a change to it is a finding the
    # `python-tests` rule would have to be loaded to see -- which it is not, and
    # deliberately so: adding it here would load the test rules on every version
    # bump. The `packaging` rule file states the requirement instead.
    RuleSpec(
        name="packaging",
        patterns=(
            "pyproject.toml",
            "requirements.txt",
            "Dockerfile",
            ".dockerignore",
            # The declared interface with the environment: every setting the
            # server reads, and the closest thing this repository has to a
            # configuration contract.
            ".env.example",
        ),
        pulls_in=("mcp-server",),
    ),
    # Everything under .github: the review workflow, these scripts, the prompts,
    # the schema and the rule files themselves.
    RuleSpec(
        name="ci",
        patterns=(".github/**",),
    ),
    # README.md is not documentation *about* this repository, it is the closest
    # thing it has to a specification: the tool contract, the client-by-client
    # setup, the transport and allowlist behaviour, and the troubleshooting that
    # tells a user what a `403` means. A change to behaviour that does not move it
    # is drift. `SECURITY.md` is the same for the threat model.
    #
    # `.system_design/` is matched although this repository has none today. The
    # pattern costs nothing and covers the directory the day it appears; the
    # alternative is a design document landing with no rule file selected, which
    # is exactly the shape upstream recorded as a defect (`contracts/` matched
    # nothing for months while 397 files accumulated).
    RuleSpec(
        name="docs",
        patterns=(
            "README.md",
            "SECURITY.md",
            "CLAUDE.md",
            "AGENTS.md",
            "**/CLAUDE.md",
            "**/AGENTS.md",
            ".system_design/**/*.md",
            "**/.system_design/**/*.md",
            # Runnable documentation. `examples/script_run_mcp_tools.py` is the
            # worked call sequence README points at, so it is prose that happens
            # to execute -- not a test, and no `tests/` glob was going to reach
            # it.
            "examples/**",
            # The images README renders. A broken or replaced asset is a
            # documentation defect and nothing else selects it.
            "assets/**",
        ),
    ),
)


def _matches(path: str, pattern: str) -> bool:
    """Report whether a repository path matches a glob pattern.

    ``fnmatch`` treats ``*`` as crossing directory separators, which would make
    ``src/pkg/search/**`` also match a sibling whose name merely starts with
    ``search``. The prefix check below keeps sibling directories disjoint.

    Args:
        path: Repository-relative path, forward-slashed.
        pattern: Glob pattern from a :class:`RuleSpec`.

    Returns:
        True when the path is covered by the pattern.
    """

    # Anchor on the literal prefix before the first wildcard so that
    # "content/**" cannot leak into a directory sharing that prefix.
    head = pattern.split("*", 1)[0]
    if head and not path.startswith(head):
        return False
    return fnmatch.fnmatch(path, pattern.replace("**/", "*"))


def select(changed_files: list[str]) -> list[str]:
    """Choose the rule files that apply to a set of changed paths.

    Args:
        changed_files: Repository-relative paths changed by the pull request.

    Returns:
        Rule names in :data:`RULE_SPECS` order, de-duplicated, including any
        pulled in by the fan-out rule.
    """

    selected: set[str] = set()

    # Direct matches first, then fan-out, so a shared-module-only change still
    # activates its consumers even though no file under them changed.
    for spec in RULE_SPECS:
        if any(_matches(p, pat) for p in changed_files for pat in spec.patterns):
            selected.add(spec.name)
            selected.update(spec.pulls_in)

    return [spec.name for spec in RULE_SPECS if spec.name in selected]


def resolve_paths(names: list[str], rules_dir: Path = RULES_DIR) -> list[Path]:
    """Map rule names to existing rule files.

    Args:
        names: Rule names returned by :func:`select`.
        rules_dir: Directory holding the rule Markdown files.

    Returns:
        Paths that exist on disk, in the order given.
    """

    resolved = []
    for name in names:
        candidate = rules_dir / f"{name}.md"
        if candidate.is_file():
            resolved.append(candidate)
        else:
            print(f"warning: rule file missing: {candidate}", file=sys.stderr)
    return resolved


def main() -> int:
    """Run the selector as a command-line tool.

    Reads changed paths from a file or stdin and writes the selected rule names
    and paths, optionally appending them to a GitHub Actions output file.

    Returns:
        Process exit code; always 0 because an empty selection is valid.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        help="File containing one changed path per line; reads stdin when omitted.",
    )
    parser.add_argument("--rules-dir", default=str(RULES_DIR))
    parser.add_argument("--github-output", help="Path to $GITHUB_OUTPUT.")
    args = parser.parse_args()

    raw = (
        Path(args.changed_files).read_text(encoding="utf-8")
        if args.changed_files
        else sys.stdin.read()
    )
    changed = [
        line.strip().replace("\\", "/") for line in raw.splitlines() if line.strip()
    ]

    names = select(changed)
    paths = resolve_paths(names, Path(args.rules_dir))

    print(f"changed files: {len(changed)}")
    print(f"selected rules: {', '.join(names) if names else '(none)'}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"rule_names={json.dumps(names)}\n")
            handle.write(f"rule_paths={' '.join(str(p) for p in paths)}\n")
            handle.write(f"rule_count={len(paths)}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
