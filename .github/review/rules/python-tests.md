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
  `test_nodriver_worker_sandbox.py` and `test_worker_launch_args_redaction.py`
  assert over the *arguments and decisions*, not over a running browser. Keep new
  tests on that side of the line.

## The guard tests — treat these as load-bearing

Five tests exist to hold an invariant that nothing else enforces. A pull request
that changes what they guard, without changing them, is a finding; a pull request
that *weakens* one to make a change pass is a critical finding.

| Test | What it holds |
|---|---|
| `test_dependency_constraints.py` | the dependency bounds in `pyproject.toml`, including `mcp>=1.25,<2` and that directly-imported packages are declared |
| `test_provider_registry_consistency.py` | `PROVIDERS` stays in step with the router, the startup check and the documentation |
| `test_tool_descriptions.py` | the tool docstrings the calling model receives |
| `test_diagnostics_masking.py` | that credentials do not reach diagnostics output |
| `test_pytest_configuration.py` | that `[tool.pytest.ini_options]` matches TEST_SUITE.md §10.5, that the running pytest actually loaded it, and that `strict_markers` / `asyncio_mode` have their effect and not merely their declaration |

Plus `test_worker_launch_args_redaction.py` for the subprocess command line — the
same class of guard, one layer down.

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
