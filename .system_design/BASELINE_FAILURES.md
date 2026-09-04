# Baseline failures — the ledger

**Status:** Drained. This file recorded the suite's red baseline one node id at
a time, so that the repairs in E1-2 through E1-5 could be checked rather than
believed. Both blocks are now empty — E1-5 removed the last id. The guard stays:
an empty ledger compared against an empty failing set is the cheapest assertion
available that the suite is green, and it costs one child run.

`TEST_SUITE.md` §1.1 quotes the baseline as counts. A count is not a checkpoint:
`12 failed` before a repair and `11 failed` after it is equally consistent with
"one test was fixed" and with "two were fixed and a third was broken". Only the
set of failing node ids distinguishes those, so the set is what this file holds
and what `tests/test_baseline_failure_ledger.py` asserts against a live run.

## How this file drains

Every entry below is a test that fails today and is expected to. **E1-2 through
E1-5 each delete exactly the ids they repair and add none.** E1-6 — the
suite-green milestone — needs both blocks empty.

**It needs more than that, and the milestone proved it.** This paragraph used to
end "the document and the milestone are therefore the same fact, checked once."
That was false, and a real run is what showed it: at commit `38026ae` both blocks
were empty, the guard was green on Linux, and **the suite was red on Windows** —
because the failing test was newer than the platform's only measurement and no
lane had run since. An empty ledger is a **necessary** condition for the
milestone, never a sufficient one. It can only ever assert something about the
platform the guard is running on, and a claim about a platform is worth exactly
one run on it. See *"Verified — the suite-green milestone"* below.

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
that needs no supporting document. **E1-4 confirmed the prediction:** it replaced
three hand-rolled doubles with the canonical typed fake under the same three node
ids, and the guard — run with the tests repaired and this ledger not yet drained
— named exactly those three under *"In the ledger, ran, and passed"* and nothing
under the other three categories. That intermediate evidence is regenerable at
any later date from `git show <this step's parent>:.system_design/BASELINE_FAILURES.md`,
which is strictly better than E1-2's one-off hand-run, and is the pattern E1-5
should follow.

**E1-5 renamed, as this paragraph predicted it would.** A concurrency test made
OS-neutral cannot keep a name saying `on_windows`, so its id left by deletion.
It built the machinery asked for here: *"Relocated claims"* below is a fenced
block of `<retired id> -> <replacement id>` rows, and the guard asserts each
replacement against the same child run — collected, and in neither the failed nor
the skipped set — and each retired id absent from the collected set. Two of its
three rows were never listed in a platform block at all; that section explains
why they are covered anyway.

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

**It has now paid for itself once, measurably.** On the first Windows run in this
repository's history it **classified** the regression — plain `pytest` reported
the failing test too, so detection is not the claim. What the guard added is the
judgement: it placed the failure under *"Failing but not in the ledger (a
regression; fix the test, do not add the id)"* rather than leaving a reader to
decide whether an unfamiliar red test on an unmeasured platform was a baseline
entry. The instruction in that message is what was followed.

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

## How each block was drained

Both blocks are empty. This is their final form, and what it records is **how**
each was drained, per platform — whether on a run or on an argument. Those are
not the same evidence, and the suite-green milestone depends on telling them
apart.

| Step | Ids drained | Linux | Windows |
|---|---|---|---|
| E1-2 | 5 (4 on Windows) | live run | argument |
| E1-3 | 3 | live run | argument |
| E1-4 | 3 | live run | argument |
| E1-5 | 1 | live run, plus a substitution row below | argument, **plus** the substitution row, which is not platform-gated |

**Linux is measured.** Every drain above was taken with the guard's own live
comparison green on Linux, which is the check this document exists to make.

**Windows is an argument, and here it is, once.** No Windows lane exists until
the CI epic, so the honest options were to drain on a stated argument or to leave
Windows carrying ids the code no longer fails on. The argument is the same each
time, and it is a claim about the *repairs* rather than about the platform: none
of them **asserts a value computed by** a branch reading `os.name` or
`sys.platform`. The stronger phrasing — that no repair *touches* such a branch —
would be false, and precision matters here because this sentence is the whole of
the Windows evidence: E1-4's spawn path does execute `os.name != "nt"` in
`_subprocess_launch_options`, which on Windows adds `creationflags` and
`startupinfo` to the very call those tests inspect. They survive it because they
assert argv and environment membership rather than the whole keyword dictionary.
E1-2's five and
E1-3's three assert resolver behaviour with the ambient state supplied by the
test rather than by the runner. E1-4's three patch
`asyncio.create_subprocess_exec`, so no process is spawned and every value they
assert — the `-m` worker module in argv, `PYTHONPATH` in the child environment,
`--browser-executable-path`, the loopback `NO_PROXY` entries — is computed by the
same platform-independent code on both. **E1-5's one is the strongest of the
four**: the test it retires failed *because* it faked a platform, its replacement
patches no platform attribute at all, and `_resolve_web_search_max_concurrency`
reads one environment variable and one integer and nothing else.

**E1-6 took its Windows figure from a real run, and the run disagreed with the
milestone claim** — with *"the suite is green"*, not with the drain argument
below, which held in every particular. That was the point of insisting on one: a
drained document is evidence of nothing on a platform nobody ran, and with the
two blocks identical the cross-platform difference check below is a tautology.
Distinguishing a run from an argument, above, is the whole reason this section
survived to the
milestone, and it earned its keep — the first Windows run in this repository's
history found a real failure. See *"Verified — the suite-green milestone"* below
for what it found and what it now records.

**The argument above was sound; it was also incomplete, and the distinction
matters.** Every claim it makes about E1-2 through E1-5's *repairs* held up on
Windows. What it could not cover is a test written **after** the baseline run and
never executed on the platform at all — which is exactly what failed. An argument
about the repairs is not an argument about the suite, and only a run is.

**One claim went uncovered with E1-5, deliberately, and E1-6's run covered it
once.** The three retired concurrency tests were, between them, the only thing in
the tree that would notice an `os.name` branch reappearing in that resolver.
E1-6's real Windows run covered it: the resolver produced identical results on
both platforms, so no platform branch had reappeared as of that run. **The cover
is not standing** — it was one run, nothing re-derives it until the CI matrix, and
nothing asserts the branch's absence in the meantime.

**The five-row relocation table above is not machine-checked, and stays that
way.** It maps each retired id to the *function* its claim moved to. Converting
it to the form below would mean choosing which node in
`tests/test_nodriver_worker_launch_resolvers.py` carries each claim — several of
those replacements are parameterized, so their node ids carry parameter suffixes,
and one claim was split across two repair steps. Deciding that now means
asserting a mapping this step never measured, which is attributing to another
step's run a fact that run did not produce: the failure the `Result:`/
`Remaining:` split exists to prevent, committed in a table instead of in a
number. So it is declined, in writing, and **no later step is scoped to convert
it**. (*"Below"* here means *"Relocated claims"*, two sections down; the
*"Verified"* section now between them is a record of two runs and is explicitly
not machine-checked.) Those five rows stand as prose backed by E1-2's one-off
hand-run, which is what they have always been.

**That decline used to rest on "E1-6 has no diff", and no longer can**, because
E1-6 took one. The reason above is the real one and always was: the mapping was
never measured, and no amount of diff budget in a later step conjures a
measurement of a run that already happened. Re-grounded here rather than left
resting on a premise the milestone falsified — a conclusion propped up by a
stale fact is how a deliberate trade-off gets mistaken for an accident. The
block two sections down is machine-checked and says so.

## Verified — the suite-green milestone

Both platforms, measured. This section is the milestone's evidence and is the
only place in this document where a **Windows figure comes from a run** rather
than from the argument recorded above.

It carries no `Result:` bullet, deliberately: `_documented_platform_headings`
identifies a platform section by exactly that line, so a bullet of that shape
here would manufacture a phantom platform and fail
`test_ledger_documents_every_platform_the_guard_knows`. The same constraint the
*"Relocated claims"* section below records, for the same reason.

Both were taken at the tree of commit **`926a57a`**, this step's last
substantive commit. A figure that cannot be pinned to a tree cannot be
re-derived or falsified, which in a document arguing that a platform claim is
worth exactly one run would be self-defeating.

- **Linux** · 2026-09-02 · commit `926a57a` · Ubuntu 24.04.4 LTS ·
  Linux 6.8.0-138 · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  hypothesis 6.167.1 · mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 ·
  nodriver 0.50.3 —
  **0 failed, 449 passed, 2 skipped, 16 subtests passed.**
- **Windows** · 2026-09-02 · commit `926a57a`, run as probe commit `9c6d2b4` ·
  Windows Server 2025 (10.0.26100) · GitHub Actions `windows-latest` ·
  CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 · hypothesis 6.167.1 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3, read from the
  run's own `pip list` —
  **0 failed, 449 passed, 2 skipped, 16 subtests passed.**

`9c6d2b4` is `926a57a` plus the temporary workflow file and nothing else,
verified with `git diff --stat` before the run and quoted in the pull request. It
is named rather than hidden because the sha the runner reported is the one a
reader would look for, and it no longer exists — the branch was deleted, as the
instrument requires.

**The commit recording these figures is necessarily later than `926a57a`**, since
a run cannot name the commit that records it. Its diff against `926a57a` is this
subsection alone. The suite was re-run on both platforms after every
review-driven change rather than once at the start: this document is parsed by
the guard, so a documentation edit really can move the result, and a figure
pinned to a superseded tree is the failure the sha exists to prevent. Stated because this document treats an unpinnable measurement
as no measurement, and the same standard applies to its own bookkeeping.

The two runs agree exactly, including the subtest count, which is a stronger
result than the milestone required and is worth stating: the suite is not merely
green on both, it is green in the same shape on both. That reconciliation is the
check worth making on any recorded pair — a divergence would mean a test collects
on one platform and not the other, which is the condition this milestone is the
last opportunity to notice before the CI matrix exists.

**Both figures are one-off developer-driven runs, and the Linux one is no more
routine than the Windows one.** `ci.yml` states in terms that it does not run
`pytest`; there is no lane on either platform. Neither number is re-derived by
anything until the CI epic.

Measured on the same throwaway-branch instrument the Windows baseline used — a
temporary workflow on a branch of its own, deleted once the numbers were
recorded. It touched neither `ci.yml` nor any workflow region the implementation
plan serialises, and **it is not the Windows CI lane**; that still arrives with
the CI epic. Nothing re-runs these two figures, which is why the milestone is
immediately followed by the workflow that makes them enforceable.

### What the first Windows run found

**It was red**, and the failure was real:

```console
2 failed, 446 passed, 2 skipped, 16 subtests passed
FAILED tests/test_nodriver_worker_sandbox.py::TestNodriverWorkerSandbox::test_backoff_grows_with_each_attempt_and_scales_for_snap
FAILED tests/test_baseline_failure_ledger.py::test_live_outcome_matches_the_ledger
```

The second is this guard, reporting the first under *"Failing but not in the
ledger (a regression; fix the test, do not add the id)"*. It is one defect, not
two, and the guard behaved exactly as designed on a platform it had never run on.

**The id was not added to the Windows block.** The instruction in the guard's own
message is the rule this document states — a new red test is a regression, and
this ledger holds what is left of the pre-existing twelve. The block stayed empty
and the test was repaired.

**Root cause, established on the platform rather than argued.** The test drove
the retry backoff `base * 2**attempt * snap_multiplier` and expected the snap
multiplier to apply, which needs `_is_snap_browser` to answer `True` for a
`/snap/`-prefixed path. That function calls `os.path.realpath`, which is
platform-dependent. A probe step in the same temporary workflow ran the shipped
function on the runner:

```console
'/snap/bin/chromium-for-tests' -> realpath='D:\snap\bin\chromium-for-tests' is_snap=False
```

A path with a single leading slash is not absolute on Windows, so `realpath`
joins it against the current working drive and normalises the separators — the
`/snap/` marker cannot survive and **no path whatsoever classifies as snap
there**. The `D:` above is whichever drive the checkout sat on, not a constant. The
recorded delays were the un-multiplied series. Production is correct and was left
alone — snap is a Linux packaging format — so the defect was the test's, and the
repair injects the classification through a seam instead of deriving it from the
host's path semantics. The wiring that remains asserted, and the coverage given
up, are stated in that test's docstring and in `TEST_SUITE.md`.

The probe output above is a measurement of the function as it was shipped on
2026-09-02 and is left exactly as recorded. The *answer* it reports is still
production's answer — nothing classifies as snap away from POSIX — but the
mechanism behind it is not: a later change repaired a separate Ubuntu
misclassification by testing the `/snap/` marker on the path as given, which
would have made this path classify as snap, so an explicit `os.name` guard now
carries the Windows answer. `TEST_SUITE.md` §3.1 has the detail. Nothing in this
document's figures changes.

**No relocation row was added, and none is needed.** The test was rewritten in
place under its existing node id, which is the pattern this document says to
prefer: the id never left the collected set, so nothing left this ledger by
deletion and *"Relocated claims"* is untouched. Stated because it is the first
question this document trains a reader to ask.

**Why nothing caught it earlier, and why that is the lesson.** The test landed
2026-09-01, at commit `a95462e`; the Windows baseline was measured 2026-08-31 at
commit `3af0563`. It is **newer than the only Windows run this repository had
ever had**, and no lane has run since. The drain argument above covers the
*repairs* E1-2 through E1-5 made, and every part of it held. It could not cover a
test written after the measurement and never executed on the platform, because no
argument can. That gap closes only with a Windows lane, not with a better
argument.

## Verified — the browser-orphan fix, both platforms

The second run in this repository's history to produce a Windows figure. It
exists because that fix makes a **per-platform behavioural claim**: the Windows
half of `_terminate_process_tree` — `taskkill /T /F` promoted ahead of
`terminate()` — is code no Linux run executes at all, and its correctness had
been argued from CPython's source rather than measured. A Linux-only check
passes while half the defect ships, which is how the Windows half of that defect
survived its first review.

Carries no `Result:` bullet, for the reason the milestone section above records.

Taken at commit **`36c8397`**, the fix's last commit.

- **Linux** · 2026-09-04 · commit `36c8397` · Ubuntu 24.04 · Linux 6.8.0-138 ·
  CPython 3.13.15 —
  **0 failed, 568 passed, 2 skipped, 16 subtests passed.**
- **Windows** · 2026-09-04 · commit `36c8397`, run as probe commit `8c18ffa` ·
  Windows-2025Server-10.0.26100-SP0 · GitHub Actions `windows-latest` ·
  CPython 3.13.15 (MSC v.1944 64 bit) · pytest 9.1.1 —
  **0 failed, 567 passed, 3 skipped, 16 subtests passed.**

`8c18ffa` is `36c8397` plus the temporary workflow file and nothing else,
verified with `git diff --stat` before the run.

**The platforms differ by one case, and the difference is accounted for rather
than tolerated.** `test_the_descendant_joins_the_childs_group_unless_asked_for_its_own`
skips on Windows: it compares process groups, and `os.getpgid` does not exist
there. 568 − 1 = 567, and 2 + 1 = 3. Any other arithmetic would mean a case was
silently not collected, which is the failure an "identical on both platforms"
figure is normally what proves — so where the figures cannot be identical, the
single named difference has to be.

**What this run actually buys.** Four subsystem cases drive a real process tree
through `_run_worker_command` — a cancelled run, a timed-out run, a descendant
in the caller's own process group, and a control that must survive. On Windows
they execute the `taskkill` branch for real. Before this run, that branch's
ordering was pinned only by an AST assertion, which proves the code is *arranged*
correctly and cannot prove it *works*. Both are needed and neither substitutes
for the other.

## Relocated claims

A retired node id and the id that now carries its claims, one row per
retirement. `tests/test_baseline_failure_ledger.py` asserts every row against the
same child run it already makes: the replacement must have been **collected** and
must be in neither the **failed** nor the **skipped** set, and the retired id
must no longer be collected at all.

That is what closes the loophole in *"An id may leave this ledger only by
passing"*. The guard's complaint about a listed id that vanished fires only while
the id is still listed, so deleting a test and its ledger entry in one change is
otherwise indistinguishable from repairing it — a dropped claim and a repair look
identical from outside.

**Two of the three rows were never in a platform block**, and are here anyway.
`..._limited_by_num_results_on_windows` and `..._defaults_on_non_windows` passed:
they faked a platform the resolver does not read, and their inputs happened to
make the expected values come out right regardless. They are retired by the same
change and for the same reason as the failing one, and a row costs one line — so
their deletion is a machine-checked fact rather than a sentence in a pull
request.

**Rows match on the exact node id.** A relocation whose replacement is
parameterized would need prefix matching, and none is; the parser rejects a
malformed row rather than guessing.

**This section must carry no `Result` bullet of the kind each platform section
opens with, and must not be moved between a platform heading and that platform's
own bullets.** `_documented_platform_headings` identifies a platform section by
exactly that line, so either edit would manufacture a phantom platform and fail
`test_ledger_documents_every_platform_the_guard_knows` — the right outcome, but
from a long way from the edit that caused it.

```text
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_defaults_on_non_windows -> tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_resolves_from_environment_and_result_count
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_defaults_on_windows -> tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_resolves_from_environment_and_result_count
tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_limited_by_num_results_on_windows -> tests/test_server.py::TestWebSearchTool::test_web_search_concurrency_resolves_from_environment_and_result_count
```

**A row may be retired** once the id on its left has been absent from the
collected set for one landed release *and* the claim has a documented home. This
is not housekeeping: E5-3 owns exhaustive coverage of
`_resolve_web_search_max_concurrency` and may absorb or rename the replacement
named above. Without a stated exit this guard would go red for a correct change,
and the only apparent remedy would be weakening a guard — which
`.github/review/rules/python-tests.md` treats as a critical finding. If E5-3
absorbs that table, it retires these rows in the same change.

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
satisfy. That was the whole of the Windows evidence until the milestone, which is
why E1-6 was forbidden to read "0 failed on both platforms" off this document.
**It did not: it ran the suite on Windows, found a failure this section could
never have shown, and recorded the result in *"Verified — the suite-green
milestone"* above.** The warning is kept in the
past tense rather than deleted, because the same hole reopens the moment a test
is added that no Windows lane executes — which is precisely how the failure got
in, and it stays open until the CI epic's matrix exists.

```text
```

## Linux

- **Measured:** 2026-08-31 · commit `3af0563` · Ubuntu 24.04.4 LTS ·
  Linux 6.8.0-138 · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3
- **Result:** 12 failed, 303 passed, 2 skipped, 9 subtests passed
- **Remaining:** 0 failed

This is the figure §1.1 predicted from source and labelled unmeasured. It was
measured, and it matched: twelve, being the eight stale `_fetch_html` callers,
the three stale loader tests and the one obsolete Windows concurrency test.

Five have since left with E1-2, the flag-and-default half of the eight; three
more with E1-3, which repaired the orchestration half in place; three more with
E1-4, which rebuilt the loader doubles on the canonical typed fake; and the last
with E1-5, which retired the obsolete Windows concurrency test into an OS-neutral
replacement — hence none remaining, and an empty block below. The **Result** line
keeps saying twelve because that is what the run said.

```text
```

## Windows

- **Measured:** 2026-08-31 · commit `3af0563` · Windows Server 2025
  (10.0.26100) · CPython 3.13.15 · pytest 9.1.1 · pytest-asyncio 1.4.0 ·
  mcp 1.29.1 · starlette 1.6.0 · uvicorn 0.52.4 · nodriver 0.50.3
- **Result:** 11 failed, 303 passed, 3 skipped, 9 subtests passed
- **Remaining:** 0 failed

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

Four of those eleven left with E1-2, three more with E1-3, three more with E1-4
and the last with E1-5, hence none remaining. **All eleven left by inference, not
by a run** — that argument is stated once, in *"How each block was drained"*
above, and is not repeated here. The *end state* it argued for has since been
confirmed by a real Windows run, recorded under *"Verified — the suite-green
milestone"*; the drains themselves were still taken on the argument, and that
distinction is kept rather than smoothed over. What the argument rests on is that
no repair **asserts a value computed by** a branch reading `os.name` or
`sys.platform` — the precise phrasing the section above uses, restored here,
where this sentence had kept the looser "touches" wording that section explicitly
records as false. E1-5's id is the one that most
obviously could have: it left because the test *faked* a platform the resolver
never reads. **The frozen `3 skipped` is now
stale by one:** the third skip *was*
`test_forces_sandbox_off_when_running_as_root`, which E1-2 deleted. It is left
as measured, because editing a measurement to keep it plausible is the failure
mode the two-line split exists to prevent. There is no Windows lane in CI until
the CI epic, so this section cannot be re-measured here.

```text
```
