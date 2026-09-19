# System design — kindly-web-search-mcp-server

**Scope of this document today: the pooled-browser lifecycle, and how the project
is distributed.** It is deliberately partial. `.system_design/` already holds the test suite's design
(`TEST_SUITE.md`) and its plan; this file is the home for *production* design,
seeded with the one area that had none and needed it — the contract by which the
parent process, the Chromium pool and the nodriver worker share a long-lived
browser. Extend it as work reaches other areas; do not treat an absent section as
a statement that an area is simple.

---

## 1. Pooled browser lifecycle

### 1.1 The parties

Browser reuse (`KINDLY_NODRIVER_REUSE_BROWSER`, on unless explicitly disabled)
keeps Chromium alive between `get_content` calls instead of paying a cold start
per request. Four components share that browser, and each holds an obligation the
others depend on:

| Component | File | Obligation |
|---|---|---|
| Parent fetch | `scrape/universal_html.py` (`fetch_html_via_nodriver`) | Acquire a slot, spawn the worker, restart the slot on a failure that indicates a poisoned browser, release the slot exactly once |
| Pool | `scrape/chromium_pool.py` (`ChromiumPool`, `ChromiumSlot`) | Launch Chromium, hand out one slot at a time, probe liveness before reuse, terminate on request |
| Worker | `scrape/nodriver_worker.py` (`_fetch_html`, reuse branch) | Connect to the slot's DevTools endpoint, obtain a page target, navigate, extract, and leave the browser as it found it |
| Chromium | — | Outlive its last tab, and refuse a tab with no window to put it in |

The worker runs in a **child process**; everything it knows about the slot
arrives as command-line arguments (`--remote-host`, `--remote-port`,
`--reuse-browser`, `--user-data-dir`). It never starts or stops the pooled
browser: `_cleanup(stop_browser=False)` is the reuse path's exit.

### 1.2 Obtaining a page target

`_ensure_reuse_page` (nested in `_fetch_html`) reuses the pooled browser's first
page target when there is one, and creates one otherwise. Both halves are live:

- **Reuse.** The ordinary first request in a session finds the tab Chromium
  opened at launch.
- **Create.** Every request after the first, because `_cleanup` closes the tab
  it navigated and that was the browser's only page target.

**Why creation goes through `browser.send`:** `nodriver.Browser` subclasses
`Connection`, and nodriver's own `Browser.get()` sends its `create_target` that
way. `Browser.connection` is *not* a usable seam — `Browser.__init__` assigns
`self.connection: Connection = None` and nothing in the package ever reassigns
it, so `browser.connection.send(...)` raises `AttributeError` on every call.
Verified by reading nodriver 0.50.3. **That is the only version it is verified
against.** `pyproject.toml` allows `>=0.50,<1`, and that `<1` bound protects
nothing here: it was assumed rather than tested, and a pre-1.0 library is free
to change this between 0.50.3 and 0.99. Any such release could reintroduce a
live `connection` attribute or change how `Browser.get()` sends, and nothing
would notice until a pooled fetch failed —
`tests/test_worker_pooled_target.py` doubles the browser, so it cannot see a
library change either. **Re-read `Browser.get()` when bumping nodriver.**

**Why `new_window=True`:** the create branch is reachable only when there are no
page targets, and Chromium closes a window when its last tab closes. A
`Target.createTarget` with `newWindow=false` then has no window to put the tab in
and is refused with `Failed to open new tab - no browser is open`. Asking for a
window costs nothing when one exists and is the only form that works when none
does, so it is unconditional rather than probed — a probe would add a CDP
round-trip and a second path to guard the one state the branch already knows it
is in.

**It constrains no binary — checked, because the protocol reference reads as if
it did.** The CDP docs mark `newWindow` "not supported by headless shell", and
the browser binary is operator-selected (`KINDLY_BROWSER_EXECUTABLE_PATH`,
`BROWSER_EXECUTABLE_PATH`, `CHROME_BIN`, `CHROME_PATH`), so a headless-shell
binary looked like a way to make every pooled request fail. It is not:
*unsupported* there means the windowing semantics are not honoured — the shell
has no windows and hands back a tab either way — **not** that the call errors.
Measured, see §1.3. `new_window=True` is accepted by every binary measured and is
the only value that works on full Chrome, so there is nothing to guard and no
fallback path to maintain.

`enable_begin_frame_control=True` is kept on the call because nodriver's own
`Browser.get()` sends it, and removing it changes nothing: measured, every
`newWindow` outcome is identical with and without it. The protocol reference
marks it headless-shell-only, so it has no consumer here; it stays only to keep
the call identical to the library's, and removing it would be safe.

### 1.3 Chromium behaviours this design rests on

None of these is in this repository's code, so none can be asserted with the
browser doubled. Measured on Chrome 153 under `--headless=new` on Windows
(2026-09-16) and reported on snap Chromium 152.0.7977.64 on Linux in issue #96:

1. Closing the last page target leaves the browser **process alive** and its
   DevTools HTTP endpoint answering.
2. `Target.createTarget` with `newWindow=false` and no window open is refused
   with `Failed to open new tab - no browser is open`.
3. `newWindow=true` is accepted in that state and leaves no window behind across
   repeated create-and-close cycles.
4. A window created this way has the **same viewport** as a tab created inside an
   existing window — outer 1920x1080, inner 1904x985, from the pool's
   `--window-size=1920,1080`, with no `width`/`height` passed to
   `Target.createTarget`. A negative result, recorded because making a dead
   branch live invites exactly this question: requests 2+ render as request 1 does.
5. **`chrome-headless-shell` accepts `newWindow=true`**, despite the protocol
   reference marking it unsupported there. Measured per binary, in the
   zero-page-target state, with and without `enable_begin_frame_control` (which
   changed no outcome):

   | Binary | `newWindow=true` | `newWindow=false` |
   |---|---|---|
   | `chrome-headless-shell` 145.0.7632.6 | accepted | accepted |
   | same, plus `--headless=new` | accepted | accepted |
   | Chrome 153.0.8010.36, `--headless=new` | accepted | **refused** |

   So `true` is accepted everywhere measured and is the only value that works on
   full Chrome. This is what retired the "the pooled path requires full Chrome"
   claim an earlier draft of §1.2 carried.

(1) is what makes the pool's health probe insufficient on its own: `ChromiumSlot.
ensure_started` probes `/json/version`, which answers for a browser with no
tabs, so a windowless slot is handed out as healthy. That is *correct* given (3)
— the worker can always make itself a window — but it is the reason the create
branch must work rather than being a rarely-taken fallback. Should a future
Chromium exit on last-tab-close instead, the probe sees `proc.returncode is not
None` and relaunches silently, and reuse again degrades to a cold start per
request with no error anywhere. A `chromium`-lane test asserting browser-pid
stability across two pooled fetches is the check that would catch it;
`TEST_SUITE.md` §9 carries that gap.

### 1.4 Failure handling, and what the string list really covers

The parent decides whether a worker failure poisoned the browser by matching a
fixed list of substrings against the exception's whole `__cause__`/`__context__`
chain (`_pool_error_requires_restart`). On a match it terminates the slot,
releases it, re-acquires and re-runs the worker exactly once.

**The worker's own wording is not load-bearing, and it is worth knowing why
not.** The worker is a *subprocess*: `_run_worker_command` turns any nonzero exit
into `RuntimeError(f"nodriver worker failed (exit={rc}): {stderr_tail}")`, and
`"nodriver worker failed"` is the first pattern in the list. So every worker-side
failure matches on the wrapper alone, whatever the worker said. The
message-specific patterns — `"failed to create pooled target"`, `"no browser is
open"`, `"failed to open new tab"` — are **defence in depth**, covering the same
strings arriving by some other route. Renaming a worker message therefore
degrades diagnosis, not recovery.

What *does* escape the list is a failure with **no message**. See the gap below.

**A killed worker is handled by type, not by message.** `_run_worker_command`
kills the whole process tree on a timeout and on a cancellation, and re-raises.
A killed worker never ran `_fetch_html`'s `_cleanup`, so the pooled browser still
holds the page it was navigating — the slot is dirty *by construction*, and no
message says so: `TimeoutError` carries an empty string, and `CancelledError`
derives from `BaseException` and never reaches the classifier at all.

So `fetch_html_via_nodriver` records the kill where it happens —
`_run_worker_noting_kills`, which wraps **both** worker runs, the first and the
restart path's retry — and its `finally` terminates the slot before releasing it.
`ChromiumSlot.terminate` clears `proc`, and `ensure_started` relaunches a slot
whose `proc` is `None`, so the next acquirer gets a clean browser. Three details
are load-bearing:

- **Terminate before release.** Released first, the queue can hand the dirty
  browser to a waiting caller before the terminate lands.
- **The terminate is suppressed.** It runs in a `finally`, where a raise would
  replace the timeout the caller needs to see; the release still happens, because
  a dropped slot is one the pool never gets back.
- **The request is not retried.** The budget that expired is the caller's.
  Re-running the worker under a fresh `total_timeout_seconds` would turn one hung
  fetch into two. Recovery here is for the *next* request, which is all it needs
  to be — so the timeout is deliberately **not** added to
  `_pool_error_requires_restart`, whose match implies a retry.

The recovery is invisible in the result, so it emits `pool.slot_recycled`. That
is the only signal an operator has; a silent relaunch reads as slowness, which is
how the issue-#96 defect survived.

### 1.5 What reuse deliberately does not isolate

`.github/review/rules/scrape-browser.md` already states that a reused browser
carries state. Precisely: closing the tab discards **that document** and its
timers and in-flight requests; cookies, storage and service-worker registrations
live in the slot's profile directory and survive every request the slot serves.
A new window is not a new profile. `Target.createBrowserContext` is the lever if
per-request isolation is ever wanted, and nothing uses it today.

---

## 2. Distribution: the published container image

### 2.1 What the repository actually ships

The project publishes **two** artifacts from one source tree, and they version
independently:

| Artifact | Where it comes from | How a user pins it |
|---|---|---|
| Python package | `pyproject.toml` `version`, installed via `uvx` / pip | the version string |
| Container image | `.github/workflows/docker-publish.yml` → `ghcr.io/<owner>/<repo>` | the image **digest** |

They are tied together deliberately but loosely: the image carries a
`:<package version>` tag read from `pyproject.toml` at build time, so a reader can
tell which package version an image contains. The tie is a *label*, not a
guarantee of immutability — see §2.3.

### 2.2 Why the image is built for two architectures

`README.md` documents `docker run` as a supported way to run the server, so the
image has to work on the machines readers actually have. Publishing only
`linux/amd64` does not degrade on ARM, it **fails**: Docker refuses to pull an
image with no entry for the host architecture. Forcing it with
`--platform linux/amd64` runs the headless Chromium this image exists to carry
under emulation, which is the one thing the image must do well.

`linux/amd64` and `linux/arm64` cover servers, PCs, Apple Silicon Macs and ARM
cloud instances. The list is short on purpose: each platform is another emulated
build leg, and an architecture only works where both the base image and Debian's
`chromium` exist for it.

**One emulated job rather than one native job per architecture.** Free
`ubuntu-24.04-arm` runners exist, so the faster shape is available — but it costs
three jobs, digests passed between them, a second runner label in the recorded
ceilings, and a pull-request path with more than one cache scope. Measured cost of
the simple shape: **5m26s** for both architectures on a cold cache. That is the
number to revisit the trade against, not an estimate.

### 2.3 The tag contract, and the one surprise in it

- `:latest` and `:<package version>` both track the newest build of the default
  branch. **They move.** `:0.1.9` names *the newest build carrying package version
  0.1.9*, not one artifact.
- `:sha-<short>` and the `@sha256:` digest are immutable. The digest is what
  README tells readers to pin, and it is stronger than pinning a base image would
  be: it pins the whole image rather than only its base.
- The `v*` tag trigger and the `type=semver` patterns exist and **have never
  fired** — the repository has no tags and no releases. They are kept because a
  release process is the intended direction.

**Why the base image and `chromium` are deliberately unpinned.** Chromium is this
image's attack surface and it fetches arbitrary URLs on a user's behalf. A pinned
base digest makes every security update wait for somebody to notice and bump it;
`workflow_dispatch` with `no-cache:` refreshes it on demand instead. The digest
above is what gives a user reproducibility, so nothing is lost. This is an
explicit exception to `.github/review/rules/packaging.md`'s "pin what the image
installs", recorded in that file so the next reader cannot mistake it for an
oversight.

### 2.4 Reachability is not controlled by anything in this repository

GitHub creates a new container package as **private** even under a public
repository: a package inherits the repository's permissions but not its
visibility, and there is no API for it. So the first successful publish leaves an
image nobody can pull until a maintainer changes the setting by hand. This is the
only thing the repository produces whose reachability no file here controls —
which is why README documents the `denied` / `unauthorized` symptom rather than
assuming the pull works.

### 2.5 How this is kept honest

The publishing workflow gates no merge (it is one of exactly two jobs outside
`ci-required`), so its correctness cannot rely on a red check stopping a bad
change. It relies instead on offline guards in
`.github/review/tests/test_review_scripts.py` — L2 contract tests in
`TEST_SUITE.md`'s sense, pairing the workflow against itself, against `README.md`
and against the `Dockerfile`. `.github/review/rules/ci.md` § `docker-publish.yml`
names them and says what each defends.
