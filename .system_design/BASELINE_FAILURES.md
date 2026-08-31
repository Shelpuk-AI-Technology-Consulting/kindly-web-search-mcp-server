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

Adding an id here is not a way to make a new failure acceptable. A new red test
is a regression; this ledger names the pre-existing twelve, and a change that
grows it needs a reason stated in its pull request.

**The guard outlives the milestone.** Once drained, it is the cheapest available
assertion that the suite is green — an empty ledger compared against an empty
failing set — so it is kept rather than deleted with E1-6. Its cost is one extra
child run of the suite, roughly doubling wall time; that is accepted because the
alternative is trusting twelve repaired tests to stay repaired with nothing
watching them.

## What is asserted and what is provenance

`tests/test_baseline_failure_ledger.py` asserts **the set of failing node ids**
for the platform it is running on, and, on every platform, that each block is
sorted, free of duplicates, consistent with the failure count on its **Result**
line, and different from the other platform's block only in the ways the
difference section below explains.

It deliberately does **not** assert the passing, skipped or subtest counts. Those
move whenever any step adds a test, so asserting them would turn every ordinary
test-adding pull request red for a reason unrelated to what it changed. **They
are frozen to the commit named on each Result line and are not expected to match
a later run** — this very change adds tests, so the first run after it merges
already reports a higher passing count than the figures below. That is correct
and is not to be "fixed".

The failure count *is* asserted, against the length of the block. The two agree
only while no test fails more than once; a test whose subtests fail several times
counts once here and several times in pytest's own total, and a ledger recording
such a run must say so on the Result line.

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
`KINDLY_*`, the provider credentials, `PYTEST_*`, `COVERAGE_*` and the rest — so
the numbers below are the **offline** baseline and are reproducible regardless of
what a developer has exported. That is not cosmetic: measured on Linux at commit
`3af0563`, exporting `RUN_LIVE_TESTS=1` alone adds a thirteenth failure,
`tests/test_serper_live.py::TestSerperLive::test_serper_search_live`, which
reaches the network and fails on the dummy credential `tests/conftest.py`
installs. Without the sweep the guard would be red on any machine configured for
live tests.

The results are read from a plugin (`tests/_baseline_probe.py`), not from
pytest's short summary. On pytest 9.1.1 a failing `unittest` subtest prints a
*passing* dot in the progress line and a `SUBFAILED(i=2) <nodeid>` line in the
summary — never the word `FAILED`. A guard parsing `FAILED` lines would report
an empty failing set on a red suite, and two of this repository's three
`subTest` sites are in `tests/test_universal_html_loader.py`, one of the files
this ledger tracks.

## What the twelve are

Grouped by cause, so a reviewer can tell which repair step owns which id. The
grouping is by module rather than per id, because the blocks are sorted and so
already group by module — a per-id table would restate the block and could drift
from it.

| Module | Ids | Cause | Repaired by |
|---|---|---|---|
| `test_nodriver_worker_sandbox.py` | 8 | `_fetch_html` grew five required keyword-only arguments; the callers were never updated | E1-2 (flags and defaults to L1), then E1-3 (retry and cleanup) |
| `test_universal_html_loader.py` | 3 | `fetch_html_via_nodriver` streams `proc.stdout`; `_FakeProc` never grew one | E1-4 |
| `test_server.py` | 1 | asserts a Windows concurrency cap that `_resolve_web_search_max_concurrency` no longer has | E1-5 |

## Why the two platforms differ

`test_forces_sandbox_off_when_running_as_root` skips on Windows, where
`os.geteuid` does not exist (`test_nodriver_worker_sandbox.py:199`), so it can
fail only on POSIX. That is the whole of the difference; every other id fails on
both. The guard asserts this block against the actual difference between the two
blocks above, which is what catches a repair that drains one platform and
forgets the other — there is no Windows lane in CI until a later workstream, so
nothing else would.

```text
linux: tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_forces_sandbox_off_when_running_as_root
```

## Linux

- **Measured:** 2026-08-31 · commit `3af0563` · Ubuntu 24.04.4 LTS ·
  Linux 6.8.0-138 · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3
- **Result:** 12 failed, 303 passed, 2 skipped, 9 subtests passed

This is the figure §1.1 predicted from source and labelled unmeasured. It is now
measured, and it matches: twelve, being the eight stale `_fetch_html` callers,
the three stale loader tests and the one obsolete Windows concurrency test.

```text
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_allows_enabling_sandbox_via_env
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_disables_sandbox_by_default
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_errors_when_no_browser_found
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_forces_sandbox_off_when_running_as_root
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_resolves_browser_executable_from_path
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_retries_and_terminates_on_devtools_timeout
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_retries_on_failed_to_connect_to_browser
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_uses_ignore_cleanup_errors_for_profile_dir
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

Measured on a GitHub Actions `windows-latest` runner, from a temporary workflow
on a branch of its own that was deleted once the numbers were recorded. It
touched neither `ci.yml` nor any of the workflow regions §1.3 serialises, and it
is not the Windows CI lane — that arrives with the CI epic. It was an instrument,
used once.

The count confirms §1.1's prediction rather than merely repeating it: eleven, and
the eleven ids below are the Linux twelve minus the root-detection case, which
Windows skips. The two runs also reconcile arithmetically — 303 passed on both,
with the twelfth test counting as a failure on Linux and as the third skip here.

The **11 failed** total equals the number of ids listed, which is the check worth
making on any recorded run: a larger total than the list would mean a test failed
more than once, through subtests the summary reports under a different word.

```text
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_allows_enabling_sandbox_via_env
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_disables_sandbox_by_default
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_errors_when_no_browser_found
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_resolves_browser_executable_from_path
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_retries_and_terminates_on_devtools_timeout
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_retries_on_failed_to_connect_to_browser
tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_uses_ignore_cleanup_errors_for_profile_dir
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_defaults_on_windows
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_passes_browser_executable_path_when_set
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_sets_no_proxy_for_loopback
tests/test_universal_html_loader.py::TestUniversalHtmlLoader::test_fetch_html_spawns_worker_subprocess
```
