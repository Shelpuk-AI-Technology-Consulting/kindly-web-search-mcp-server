# Rule: tests (`tests/`, `.github/review/tests/`)

## The bar

- **A test that would pass if the behaviour it names were broken is not a test.**
  For each assertion, ask what change to production code would make it fail. If
  the answer is "none", say so — that is the single most valuable finding in a
  test review.
- Production code with no corresponding test is a finding. So is a test that only
  asserts the happy path of a function whose failure path is the interesting one
  (every fallback in `content/resolver.py`, every retry in `scrape/`).
- A test that asserts a mock was called, and nothing about the value produced,
  tests the test.

## This suite's conventions

- **`sys.path` insertion, not an installed package.** Both `tests/conftest.py`
  and individual test modules do
  `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))`. That is
  the convention; a new test that assumes `pip install -e .` will fail for
  everyone else.
- **Both `unittest.TestCase` and plain pytest functions are in use**
  (`test_serper_live.py` is `unittest`, others are not). Either is acceptable;
  match the file you are editing rather than converting it.
- `tests/conftest.py` sets a dummy `serper_api_key` in a session-scoped autouse
  fixture **only when one is not already present**, so unit tests are
  deterministic while live tests stay opt-in. A change that unconditionally
  overwrites it disables the live tests silently; a change that removes the
  fallback makes the unit tests depend on the developer's environment.

## Network access

- **A unit test must not reach the network.** If a new test hits a real endpoint
  it will fail in CI, fail behind a proxy, and pass or fail depending on someone
  else's rate limit. That is a finding regardless of how convenient it is.
- The live tests (`test_serper_live.py`, `test_live_fetch_urls.py`) are the
  exception and they gate themselves on a real key being present. A live test
  that runs unconditionally, or a unit test that silently becomes a live one
  because a developer had a key exported, is a finding.
- Same for the browser: a test that launches real Chromium is not a unit test.
  `test_nodriver_worker_sandbox.py`, `test_worker_launch_args_redaction.py` and
  `test_nodriver_worker_launch_resolvers.py` assert over the *arguments and
  decisions*, not over a running browser. Keep new tests on that side of the
  line. The last of those is the pattern to copy: it calls the resolvers
  directly and pins every ambient input they read — the environment variables,
  `os.geteuid` and `shutil.which` — so a flag decision costs no browser and no
  patched `sys.modules`.

## The guard tests — treat these as load-bearing

Fourteen tests exist to hold an invariant that nothing else enforces. A pull request
that changes what they guard, without changing them, is a finding; a pull request
that *weakens* one to make a change pass is a critical finding.

| Test | What it holds |
|---|---|
| `test_dependency_constraints.py` | the dependency bounds in `pyproject.toml`, including `mcp>=1.25,<2` and that directly-imported packages are declared |
| `test_provider_registry_consistency.py` | `PROVIDERS` stays in step with the router, the startup check and the documentation |
| `test_tool_descriptions.py` | the tool docstrings the calling model receives |
| `test_diagnostics_masking.py` | that credentials do not reach diagnostics output |
| `test_pytest_configuration.py` | that `[tool.pytest.ini_options]` matches TEST_SUITE.md §10.5, that the running pytest actually loaded it, and that `strict_markers` / `asyncio_mode` have their effect and not merely their declaration |
| `test_min_selected_guard.py` | that the `--min-selected` collection floor in `tests/conftest.py` matches TEST_SUITE.md §10.3 and still fires — an under-selected marker job exits 4 and names both counts, while a genuine failure keeps exit 1 and a collection error keeps its own diagnosis |
| `test_coverage_configuration.py` | that `.coveragerc`, `.coveragerc-gate` and `.coveragerc-subprocess` match TEST_SUITE.md §10.4 and still behave — the base config reports an unexecuted module at zero, the gate config omits the modules with no hermetic seam, and the subprocess config captures a child process |
| `test_worker_process_protocol.py` | that the `WorkerProcess` Protocol names exactly the surface production consumes, and that the typed double still satisfies it both at run time and statically — it shells out to mypy over four committed negative fixtures and asserts each is rejected by its own diagnostic code, so a mock substituted for the double, a double missing a member, an explicitly `Any`-typed one, and a `bytearray` stream payload all stay rejected — the last is the only thing enforcing the `strict_bytes` constraint, which no runtime test can prove. It also holds the shape to **one** definition: any class outside the two allow-listed modules that mentions the *whole* worker-process surface fails it, which is what stops a later author reimplementing the canonical double instead of importing it. Its reach is that whole-surface shape and no more — the double that caused the original outage named only `returncode` and `communicate()` and would not be reported; `DETECTOR_CASES` pins both directions |
| `test_worker_command_builder.py` | that no public callable defined in `scrape/universal_html.py` **or `scrape/worker_runner.py`** accepts a caller-supplied child command — the loader's url argument is attacker-influenced, so such a parameter on either public surface would turn "execute an arbitrary process" into a supported input. The seam is private by design: the command builder is module-private and so is the runner it feeds, which is why the assertion sweeps whole public surfaces rather than named functions. The sweep is parametrized over the two modules with **different** non-vacuity expectations — the loader must have a public surface or the sweep is looking at nothing, the runner must have none at all, which is the stronger claim and subsumes the sweep — so collapsing them into one expectation weakens both. Every callable is swept before either expectation is checked, so a public callable that takes a command is reported as a command hole rather than merely as an unexpected export. The forbidden names are a set — `command`, `cmd`, `argv`, `args`, `executable`, `program` — because the hole is the capability rather than a spelling; **variadic parameters are exempt deliberately**, so a public `*args` does not raise a false alarm on a guard whose cheapest escape would be to weaken it. Each name is verified to fire, and both exemptions to stay quiet. The rest of the module pins the worker command's exact shape by whole-list equality, pooled and unpooled; loosening those to membership to make a command-line change pass is weakening a guard |
| `test_worker_runner.py` | the module boundary between the browser loader and the parent-side process runner. `scrape/universal_html.py` must import neither `asyncio` nor `subprocess` — both are needed to start, wait on, stream from or kill a process, so their joint absence *is* the "no process management here" claim rather than a proxy for it — while still defining the command builder and the Markdown-probe path, so the split cannot be satisfied by moving everything. It also names the moved surface as a list, so a partial move back fails instead of drifting; requires the runner to expose no public callable at all; and forbids `from asyncio import create_subprocess_exec` in the runner, which would bind the spawn primitive at import time and make it unreplaceable. That boundary is what the gating coverage configuration's file-granular `omit` rests on: re-mixing the two files silently un-gates the probe path or gates code with no hermetic seam. Relaxing any of these to let process code back into the loader is weakening a guard |
| `test_baseline_failure_ledger.py` | **two** invariants. First, that `.system_design/BASELINE_FAILURES.md` names exactly the tests that fail today — it re-runs the suite in a child process with the live-test opt-ins cleared and compares node ids, so a repair that forgets to delete its ledger entry and a new red test both turn it red, and for opposite reasons it names separately. Second, that every relocation row in that document landed: the guard's first complaint can only fire for an id still listed, so deleting a test *and* its ledger entry in one change is otherwise indistinguishable from repairing it, and the `<retired id> -> <replacement id>` rows close that hole by holding each replacement to the same child run — collected, and neither failing nor skipped — while requiring the retired id to be gone. Deleting a relocation row to make a rename pass is weakening a guard. Both invariants share one child run through a session fixture, so the module still spawns exactly one |
| `test_worker_child_fixture.py` | **two of its fourteen cases only.** The other twelve calibrate a test instrument -- they drive the fixture child's flags against a real process, and a change to that script is meant to change them. The two that are load-bearing hold properties of the script rather than of any test: that `tests/child_processes/worker_child.py` imports **only** the standard library, and that pytest collects nothing from that directory. The first is what keeps the fixture startable at all -- the process runner hands its child a *complete* environment, and a path-invoked script never runs `tests/conftest.py`, so an import of the package would resolve or not depending on ambient path setup; the anti-flake harness and the lifecycle steps are both scheduled to edit that script, and nothing else enforces either property. The second has a cheap escape under a rename -- deleting the case -- and if it goes the script becomes collectable, which means executed at collection time, which means frames on the runner's stderr and possibly a hang. Weakening either is weakening a guard; editing the other twelve alongside the script is ordinary work |
| `test_searxng_contract_server.py` | **three of its eighteen cases only.** The other fifteen calibrate a test instrument -- they drive a local HTTP server that answers like a self-hosted SearXNG instance, and a change to that server is meant to change them. The three that are load-bearing are: that `tests/fixture_servers/searxng_contract.py` imports **only** the standard library; that the response it serves equals, value for value, the specimen recorded in the design document; and that every row of that document's request/answer table is driven against a live instance. The first holds two properties at once -- the instrument must not import the parser it exists to pin, which would be circular, and it must start in an environment carrying only the wheel's own dependencies, which is the job that consumes it. The second and third are what stop the fixture becoming *more permissive than a real instance*: a stand-in that answered JSON to a request the real thing refuses lets a caller pass here and fail in production, which is the one failure a stand-in exists to prevent. Both have the same cheap escape -- comparing field names instead of values, or deleting a table row -- and taking it is weakening a guard; editing the other fifteen alongside the server is ordinary work |
| `test_corpus_policy.py` | the governance of the saved-HTML corpus under `tests/corpus/html/`, which in a **public** repository is a set of published documents. It holds the policy twice -- as a module constant and as the fenced JSON block in TEST_SUITE.md section 3.3 -- and compares them both ways, so a policy edit is a reviewed two-file edit. Because both copies are edited together, the obligations the design names are pinned a **third** time against literals in the module: the sanitation categories, the two tiers, each tier's required, either-or and forbidden field lists, and the patterned fields. Without that third anchor, dropping `capture_date` from the document and the constant in one pull request leaves every parametrized sweep smaller and the suite green -- that is the specific escape, and taking it is weakening a guard. One checker implements every rule and is driven two ways: once over the committed corpus, with each case filtering to a single rule; and once per rule over a synthetic corpus in `tmp_path` that breaks exactly that rule, asserting the **exact** violation list rather than a non-empty one. Sanitation rows carry positive *and* negative specimens, as lists, each driven individually: the positives prove a row is armed for every shape a leak takes -- `Set-Cookie:` is a response header, so a row armed only against it misses the `http-equiv` meta and the `document.cookie` assignment, which are how a cookie actually reaches committed HTML -- and the negatives pin measured false positives, an API documentation heading reading `Authorization: Bearer tokens` and the `logo@2x.png` retina filename. Deleting a specimen, collapsing the lists, or loosening a row to make a capture commit is weakening a guard; the honest move is to trim the capture. The snapshot tier ships empty by design, so its committed-tree cases are vacuous until the first real snapshot lands and only the synthetic cases carry those claims until then |

Five of these import `_section_body` from a sixth. `test_min_selected_guard.py`,
`test_coverage_configuration.py`, `test_baseline_failure_ledger.py`,
`test_searxng_contract_server.py` and `test_corpus_policy.py` all take it from
`test_pytest_configuration.py` rather than keeping their own copy of the
fence-aware section bound, which exists because the naive heading regex was
measured wrong — on §10.4's own blocks, whose opening comment sits at column 0
and reads as a heading. A change to that helper's name or behaviour breaks all
five of them, so it is not the private detail its underscore suggests.

Plus `test_worker_launch_args_redaction.py` for the subprocess command line — the
same class of guard, one layer down.

`test_worker_command_builder.py` and `test_worker_runner.py` are two halves of
one boundary and should be read together: the first sweeps the *public* surface
of both modules for a caller-supplied command, the second holds the split that
makes the sweep meaningful. The sweep is parametrized over the two modules with
different expectations — the loader must have a public surface for the sweep to
be looking at anything, the runner must have none — so collapsing them into one
expectation weakens both.

## Secrets in tests

- No real key in a fixture, a recorded response, or a committed sample. A
  redacted-looking string that is actually valid is the failure mode to watch
  for.
- `test_serper_live.py` loads a `.env` with a minimal parser written
  deliberately *"avoids printing secrets"*. A change that swaps it for a library
  that logs what it loaded defeats that; a change that makes it print its parsed
  values is a critical finding.

## Async

Most of this package is `async`. Check a new async test actually awaits the thing
it claims to test — a coroutine that is created and never awaited passes silently
and asserts nothing, and Python only warns about it if the warning is not
filtered.

## The review system's own tests

`.github/review/tests/test_review_scripts.py` guards the workflow that produces
these reviews, so a silent break there disables review across the repository
without anything going red. It must stay runnable with **nothing but a Python
interpreter and no installed dependencies** — a broken review workflow has to be
diagnosable without first provisioning an environment. A new test there that
imports a third-party package breaks that property.

Run it directly, not through `unittest discover`:

    python .github/review/tests/test_review_scripts.py

`discover` imports the start directory as a package and `.github` is not a valid
package name.
