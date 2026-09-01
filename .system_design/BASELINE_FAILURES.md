# Baseline failures — the ledger

**Status:** As Is. This file records the suite's red baseline exactly as it is
today, one node id at a time, so that the repairs in E1-2 through E1-5 can be
checked rather than believed.

`TEST_SUITE.md` §1.1 quotes the baseline as counts. A count is not a checkpoint:
`12 failed` before a repair and `11 failed` after it is equally consistent with
"one test was fixed" and with "two were fixed and a third was broken". Only the
set of failing node ids distinguishes those, so the set is what this file holds
and what `tests/test_baseline_failure_ledger.py` asserts against a live run.

## How this file drains

Every entry below is a test that fails today and is expected to. **E1-2 through
E1-5 each delete exactly the ids they repair and add none.** E1-6 — the
suite-green milestone — is reached when both blocks are empty, at which point the
guard is asserting that a real run has no failures at all. The document and the
milestone are therefore the same fact, checked once.

**An id may leave this ledger only by passing.** Deleting a test, renaming its
class or marking it skipped removes it from the failing set too, and all three
look exactly like a repair from the outside. The guard therefore records the
child run's *collected* and *skipped* sets as well as its failing one, and names
those three outcomes separately. This is the direction that will actually be
tripped: a repair step rewriting eight stale tests can lose one by accident far
more easily than it can invent a new failure.

**The one exception, and why it is one: a relocated claim.** E1-2 does not
repair its five ids in place, because the layer they sit at is the defect. They
asserted flag and default resolution by driving `_fetch_html`, so each id left
by deletion, and the guard cannot see the difference between that and a claim
quietly dropped — a deletion is only visible to it while the id is still listed
here, which it is not once the same change removes it. What replaces them is
therefore recorded rather than inferred:

| Id deleted from this ledger | Claim, now asserted directly against |
|---|---|
| `test_disables_sandbox_by_default` | `_resolve_sandbox_enabled` — unset is off |
| `test_allows_enabling_sandbox_via_env` | `_resolve_sandbox_enabled` — every affirmative spelling |
| `test_forces_sandbox_off_when_running_as_root` | `_resolve_sandbox_enabled` with a patched euid, and `_build_chromium_launch_args` for `--no-sandbox` |
| `test_resolves_browser_executable_from_path` | `_resolve_browser_executable_path` with a patched `shutil.which` |
| `test_errors_when_no_browser_found` | `_resolve_browser_executable_path` — nothing found resolves to `None`, which is what `_fetch_html` turns into the error |

`tests/test_nodriver_worker_launch_resolvers.py` holds all five, and covers each
against mutation of the branch it names. Relocation is not a licence: an id
leaving this ledger without either a passing run or a row in a table like this
one is a dropped claim, whatever the pull request calls it.

E1-2 evidenced the table by hand, running the guard in the intermediate state —
tests deleted, ledger not yet drained — so that it named those five ids as *no
longer collected* and named nothing else. That is a real proof and it is also a
one-off: nothing re-runs it.

**E1-2 predicted that E1-3 and E1-4 would hit the same wall. They do not, and
the reason is worth keeping.** A step that *rewrites a test in place* keeps its
node id, so the id leaves this ledger by passing and the rule holds natively
with no exception and nothing to take on trust. E1-3 rewrote three test bodies
under their existing names and the guard reported all three under *"In the
ledger, ran, and passed"* — the strongest of its four categories, and the one
that needs no supporting document. E1-4 replaces a fixture, so it is in the same
position.

**The step to watch is E1-5**, which rewrites the concurrency test to be
OS-neutral. Its id is `test_web_search_concurrency_defaults_on_windows`, and a
name that still says `on_windows` would be wrong afterwards — so that step
probably *will* rename, and its id will leave by deletion. It is the remaining
candidate for making this table machine-readable: a fenced block of
`<deleted id> -> <replacement id>`, asserted to be in the child run's *collected*
set and absent from its *failed* set. The guard already holds both sets. If
E1-5 keeps the name instead, no such machinery is needed at all.

**The rule that generalises: prefer rewriting in place to relocating.** Not for
tidiness — it is the difference between an id that leaves this ledger under a
machine-checked category and one that leaves under a promise in a pull request.
Relocate only when the layer itself is the defect, as it was for E1-2.

Adding an id here is not a way to make a new failure acceptable. A new red test
is a regression; this ledger names what is left of the pre-existing twelve, and
a change that grows it needs a reason stated in its pull request.

**The guard outlives the milestone.** Once drained, it is the cheapest available
assertion that the suite is green — an empty ledger compared against an empty
failing set — so it is kept rather than deleted with E1-6. Its cost is one extra
child run of the suite, roughly doubling wall time; that is accepted because the
alternative is trusting twelve repaired tests to stay repaired with nothing
watching them.

## What is asserted and what is provenance

`tests/test_baseline_failure_ledger.py` asserts **the set of failing node ids**
for the platform it is running on, and, on every platform, that each block is
sorted, free of duplicates, consistent with the still-failing count on its
**Remaining** line, and different from the other platform's block only in the
ways the difference section below explains.

Each platform section therefore carries **two** summary lines, and the
distinction between them is the point:

- **Result** is the measurement. It records what the run named on the
  **Measured** line above it actually produced, and **it is never edited
  again** — not its failure count, not its passing count, not anything. A
  measurement that gets edited is no longer a measurement.
- **Remaining** is the bookkeeping. It states how many ids the block below still
  holds, it is maintained by every repair, and it is the number the guard
  asserts against the length of the block.

The first version of this document had only the **Result** line and asserted its
failure count. The reasoning was that a number read off the recorded run cannot
be quietly edited to match the list. The reasoning was right; the placement was
wrong. A repair deletes ids, so the count has to move, and moving it in place
would have produced `7 failed, 303 passed, 2 skipped` under a **Measured** line
naming a commit where no such run ever happened — three real figures and one
arithmetic edit, indistinguishable on the page. `TEST_SUITE.md` §1.1 states the
rule that forbids it: baselines are quoted from real runs, or not quoted. The
redundancy given up is small, because the live comparison checks the block
itself against a real run, which is stronger than any count.

Nothing asserts the passing, skipped or subtest counts, on either line. Those
move whenever any step adds a test, so asserting them would turn every ordinary
test-adding pull request red for a reason unrelated to what it changed. **They
are frozen to the commit named on each Measured line and are not expected to
match a later run.** Two consequences follow that are easy to misread as errors:
a later run reports a higher passing count than the figures below, and — once a
repair removes a test that used to *skip* on a platform — the frozen skip count
is stale by that test. Both are correct, and neither is to be "fixed" by editing
a measurement.

The **Remaining** count and the block agree only while no test fails more than
once; a test whose subtests fail several times counts once here and several
times in pytest's own total, and a ledger recording such a run must say so on
the Result line.

On a platform with no section here — macOS, say — the live comparison **skips**
with a stated reason. The ledger claims nothing about a platform nobody measured,
and a guard that silently passed there would be worse than one that says so. For
the same reason a platform is either measured and listed, or absent: there is no
empty-because-unknown state, because an empty block already means *drained* and
one symbol cannot mean both.

The baseline is defined for one canonical interpreter and dependency set per
platform, named on each Result line. §10.3's CI matrix also varies the Python
version and the `mcp` bound; this ledger makes no claim about those legs and
should be drained before they exist.

## The measurement command

A failing set means nothing without the selection that produced it. The guard
runs exactly this, and asserts this block against the argv it builds, so any
later change to the selection — the `live`, `chromium` and `package` markers
arrive in a later workstream — is a deliberate edit here rather than a silent
redefinition of the baseline.

```console
python -m pytest -p no:cacheprovider -p tests._baseline_probe -c <repo>/pyproject.toml --ignore=tests/test_baseline_failure_ledger.py --baseline-probe-json=<tempfile> -q --tb=no
```

Both runs recorded below predate this guard and were plain `pytest -q` on a clean
checkout. That is not a discrepancy: the flags above add a plugin, exclude the
guard's own module and pin the configuration, none of which changes *which* tests
fail — and the Linux comparison passes against the ids recorded from the plain
run, which is the check rather than the claim.

The child's environment is swept of everything this project reads —
`KINDLY_*`, `PYTHON*`, `PYTEST_*`, `COVERAGE_*` and the other project prefixes,
plus the provider credentials, the proxy variables and the browser-path
variables that carry no common prefix — so
the numbers below are the **offline** baseline and are reproducible regardless of
what a developer has exported. That is not cosmetic: measured on Linux at commit
`3af0563`, exporting `RUN_LIVE_TESTS=1` alone adds a thirteenth failure,
`tests/test_serper_live.py::TestSerperLive::test_serper_search_live`, which
reaches the network and fails on the dummy credential `tests/conftest.py`
installs. Without the sweep the guard would be red on any machine configured for
live tests.

The sweep is held honest by a check that scans the source tree for every string
literal shaped like an environment variable name and asserts none survives.
Anchoring that scan on `os.environ.get("...")` call sites was tried and measured
wrong: `CHROME_BIN`, `CHROME_PATH`, `BROWSER_EXECUTABLE_PATH` and `no_proxy` are
read through a *loop variable* over a tuple of literals, and others through
`_get_int_env(key, ...)` helpers, so no call-site pattern can see them. The shape
scan over-collects instead — the safe direction, since a false positive costs one
allow-list entry while a false negative costs a silently steerable baseline. Two
of those browser-path variables steer `_resolve_browser_executable_path`, whose
tests were two of the twelve ids this ledger opened with, so the gap would have
become a real divergence the moment those repairs landed on a machine with
`CHROME_BIN` set. Those two are now repaired, and their replacements clear all
four browser-path variables themselves rather than relying on this sweep — the
sweep protects the child run, not a test that calls a resolver in-process.

The results are read from a plugin (`tests/_baseline_probe.py`), not from
pytest's short summary. On pytest 9.1.1 a failing `unittest` subtest prints a
*passing* dot in the progress line and a `SUBFAILED(i=2) <nodeid>` line in the
summary — never the word `FAILED`. A guard parsing `FAILED` lines would report
an empty failing set on a red suite, and two of this repository's three
`subTest` sites are in `tests/test_universal_html_loader.py`, one of the files
this ledger tracks.

## What the remaining four are

Grouped by cause, so a reviewer can tell which repair step owns which id. The
grouping is by module rather than per id, because the blocks are sorted and so
already group by module — a per-id table would restate the block and could drift
from it.

| Module | Ids | Cause | Repaired by |
|---|---|---|---|
| `test_universal_html_loader.py` | 3 | `fetch_html_via_nodriver` streams `proc.stdout`; `_FakeProc` never grew one | E1-4 |
| `test_server.py` | 1 | asserts a Windows concurrency cap that `_resolve_web_search_max_concurrency` no longer has | E1-5 |

`test_nodriver_worker_sandbox.py` has left this table entirely: its five
flag-and-default ids relocated with E1-2 and its three orchestration ids were
repaired in place by E1-3.

## Why the two platforms differ

They no longer do. `test_forces_sandbox_off_when_running_as_root` was the whole
of the difference: it skipped on Windows, where `os.geteuid` does not exist, so
it could fail only on POSIX. It left with E1-2, and the claim it made is now
asserted against `_resolve_sandbox_enabled` with the euid supplied by the test
rather than by the runner — which is why the replacement runs, and fails when
the override is removed, on both platforms alike.

The section stays, with an empty block, rather than being deleted. The empty
block is the assertion that the two platform blocks are now identical, and it is
the only check that runs everywhere: only one platform's live comparison can
execute on any one machine, and there is no Windows lane in CI until a later
workstream, so a repair that drains Linux and forgets Windows would otherwise
pass everything that actually runs.

**It is a weaker check than the one it replaces, and knowingly so.** While the
blocks differed, this section pinned a verified Windows fact. Empty, it says
only that the two blocks match — which two identically *wrong* blocks also
satisfy. Combined with the Windows section being unmeasurable until the CI
epic's Windows lane exists, that means the Windows block is now asserted by
nothing that runs. E1-6 must not read "0 failed on both platforms" off this
document: the Linux half is checked against a real run, the Windows half is a
claim awaiting its lane.

```text
```

## Linux

- **Measured:** 2026-08-31 · commit `3af0563` · Ubuntu 24.04.4 LTS ·
  Linux 6.8.0-138 · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3
- **Result:** 12 failed, 303 passed, 2 skipped, 9 subtests passed
- **Remaining:** 4 failed

This is the figure §1.1 predicted from source and labelled unmeasured. It was
measured, and it matched: twelve, being the eight stale `_fetch_html` callers,
the three stale loader tests and the one obsolete Windows concurrency test.

Five have since left with E1-2, the flag-and-default half of the eight — hence
seven remaining, listed below. The **Result** line keeps saying twelve because
that is what the run said.

```text
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_defaults_on_windows
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_passes_browser_executable_path_when_set
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_sets_no_proxy_for_loopback
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_spawns_worker_subprocess
```

## Windows

- **Measured:** 2026-08-31 · commit `3af0563` · Windows Server 2025
  (10.0.26100) · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3
- **Result:** 11 failed, 303 passed, 3 skipped, 9 subtests passed
- **Remaining:** 4 failed

Measured on a GitHub Actions `windows-latest` runner, from a temporary workflow
on a branch of its own that was deleted once the numbers were recorded. It
touched neither `ci.yml` nor any of the workflow regions §1.3 serialises, and it
is not the Windows CI lane — that arrives with the CI epic. It was an instrument,
used once.

The count confirmed §1.1's prediction rather than merely repeating it: eleven,
being the Linux twelve minus the root-detection case, which Windows skipped. The
two runs also reconciled arithmetically — 303 passed on both, with the twelfth
test counting as a failure on Linux and as the third skip here. At the time of
that run the **11 failed** total equalled the number of ids listed, which is the
check worth making on any recorded run: a larger total than the list would mean
a test failed more than once, through subtests the summary reports under a
different word.

Four of those eleven left with E1-2, hence seven remaining. **The frozen
`3 skipped` is now stale by one:** the third skip *was*
`test_forces_sandbox_off_when_running_as_root`, which E1-2 deleted. It is left
as measured, because editing a measurement to keep it plausible is the failure
mode the two-line split exists to prevent. There is no Windows lane in CI until
the CI epic, so this section cannot be re-measured here.

```text
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_defaults_on_windows
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_passes_browser_executable_path_when_set
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_sets_no_proxy_for_loopback
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_spawns_worker_subprocess
```
