# Rule: documentation (`README.md`, `SECURITY.md`, `examples/`, `assets/`, `.system_design/`)

## README.md is the specification

There is no separate design document in this repository. **`README.md` is the
closest thing it has to one**, and it is what every user reads before installing:
the tool contract, the client-by-client setup for seven MCP clients, the
transport and allowlist behaviour, the proxy configuration, and the
troubleshooting that tells someone what a `403` or `421` actually means.

Judge it as a specification, not as prose:

- **A behaviour change that does not move the README is drift.** New environment
  variable, changed default, changed tool description, changed transport
  behaviour, changed allowlist handling — each has a README section, and a pull
  request that changes the code without the document is a finding.
- The reverse too: a README claim that the code does not implement is a defect in
  the README. Verify a claim before accepting it — `grep` for the variable, read
  the function. A count or a default you derived beats one you accepted.
- The **client setup blocks** (Codex, Claude Code, Gemini CLI, OpenClaw,
  Antigravity, Cursor, Claude Desktop, GitHub Copilot) are copy-paste
  configuration. A stale one does not degrade gracefully — it fails at the user's
  first launch. A change to the run command, the entry-point names in
  `pyproject.toml`'s `[project.scripts]`, or the required environment variables
  must sweep **all** of them. Check every block, not the first one.
- Version numbers, package names and command lines in the README are executable
  content. Treat a wrong one as a defect, not a typo.

## Report a documentation defect once, with every instance

The same wrong number in four client blocks is **one** finding with the other
three in `other_instances` — not four findings, and not one finding per review
round. Upstream measured a single wrong count reported seven times across four
documents, one site per round. If you cannot enumerate every instance, say so and
give the search that would find them.

## SECURITY.md

🔴 **As committed, `SECURITY.md` is the unmodified GitHub template.** It carries
placeholder instructions ("Use this section to tell people…") and an invented
version table (`5.1.x`, `4.0.x`) that matches no release of this project, which
is at `0.1.x`.

- Do **not** read it as a statement of this project's threat model or supported
  versions — it is not one, and treating it as authoritative would make you judge
  a change against a fiction.
- A pull request that replaces it with real content is an improvement; check the
  version table it lands with actually matches `pyproject.toml`.
- Do not raise the placeholder state as a new finding on every unrelated pull
  request. It is a known, pre-existing gap recorded here.

## `examples/`

`examples/script_run_mcp_tools.py` is runnable documentation — the worked call
sequence. It is prose that executes, so it must **actually run** against the
current tool signatures. A tool signature or response-shape change that does not
update it leaves a sample that fails on first use, which is worse than no sample.

It is not a test and should not be treated as one: it has no assertions and
nothing runs it in CI.

## `assets/`

The images the README renders. A replaced or removed asset that leaves a broken
reference is a documentation defect and nothing else selects it. Check the
reference in the README still resolves.

## `.system_design/`

**This repository has no `.system_design/` directory today.** The rule matches it
so that the day one appears it is not reviewed with zero rules loaded.

If a pull request adds one, judge it on whether it records the **why** and not
only the **what**: for each significant design choice, why it is done this way
and not the obvious alternative, with particular attention to anything that
departs from best practice or would surprise the next reader. A design document
that lists decisions without their justifications lets a future maintainer
mistake a deliberate trade-off for an accident.

Where such documents exist, code that contradicts them is a finding — **quote
both sides**.

## Docstrings are documentation too

This project's convention is a **Google-style docstring on every class, method
and function**, with `Args:`, `Returns:` and `Raises:`, plus a module-level
docstring on every file. The existing code follows it unevenly — `search/`,
`utils/diagnostics.py` and much of `scrape/` are well documented; `models.py`,
`settings.py` and parts of `content/` are thinner.

- Hold **changed** symbols to the convention. A new public function without a
  docstring is a finding.
- Do not require docstrings on symbols the pull request did not touch. Mention a
  pre-existing gap; do not make it a blocker.
- A docstring that no longer matches the code it sits above is worse than none —
  flag a stale one on a changed symbol as a defect, not a suggestion.
