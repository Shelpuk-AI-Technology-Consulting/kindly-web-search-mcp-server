# Automatic pull request review

Every pull request against `main` is reviewed by Claude Code, running in
`.github/workflows/claude-code-review.yml`. The review is posted as a GitHub
review with inline comments on the lines it is about, and **a pull request that
was not reviewed fails the check** — a green tick has to mean a review actually
happened.

This file is for the two people who need it: whoever sets the workflow up, and
whoever answers what it finds.

---

## What the reviewer is given

| Piece | What it is |
|---|---|
| `REVIEW_GUIDE.md` | the repository-wide contract — what this product is, what a change must never weaken, how to rank severity |
| `REVIEW_PROMPT.md` | the task: read the conversation, then `README.md`, then the diff; sweep eight dimensions; return structured output |
| `rules/*.md` | per-component rules, selected by which files the pull request touched |
| `scripts/select_rules.py` | the map from changed paths to rule files, including the fan-out that makes a shared-module change pull in its consumers' rules |
| `schemas/review_findings.schema.json` | the findings contract the model's output is validated against |

The pull request's own conversation — description, comments, inline threads and
previous rounds — is fetched and put in the prompt **before** the diff, so the
reviewer can tell an addressed finding from an ignored one. It is fenced as
untrusted input: an attempt to instruct the reviewer through a comment is itself
reported as a critical finding.

---

## Answering a review

**Reply in the thread, verify it published, then resolve.**

```bash
gh api -X POST repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies -f body='...'
gh api repos/{owner}/{repo}/pulls/{pr}/comments --paginate \
  --jq '[.[]|select(.in_reply_to_id=={comment_id})]|length'
```

🔴 **Not `addPullRequestReviewThreadReply`.** That GraphQL mutation attaches the
reply to *your pending review* — a draft only you can see — while
`resolveReviewThread` still works. The thread then reads as answered to you and
as closed in silence to everybody else. It reports success and says nothing.
Check for a `PENDING` review before resolving:

```bash
gh api graphql -f query='{repository(owner:"OWNER",name:"NAME"){pullRequest(number:PR){reviews(first:100){nodes{state}}}}}' \
  --jq '[.data.repository.pullRequest.reviews.nodes[]|select(.state=="PENDING")]|length'
```

Publish a draft you already wrote with `submitPullRequestReview(event:COMMENT)`.

⚠️ **Resolving is not answering.** The `review_replies` job in
`.github/workflows/ci.yml` blocks the merge when a resolved thread's comments are
all by one author, because that state has two causes and they are
indistinguishable from outside: the finding was dismissed without a word, or a
reply was written and never published. If a thread genuinely needs no answer —
your own question, a note on your own change — say so in the thread in one
sentence and then resolve it. That clears the check.

The gate reads the API live, so a re-run is enough; **do not push an empty commit
to clear it**, because that re-triggers the billed review.

### Disagreeing with a finding

Say so in the thread. The reviewer reads the conversation on the next round and
is told, in `REVIEW_PROMPT.md`, that an explanation may resolve a finding it
would otherwise raise — and equally that disagreement is information, not a
verdict: if the code is still wrong, the finding still stands however firmly
somebody argued.

---

## Setting it up

The reviewer is configured entirely by **organisation** Actions secrets and
variables, shared with the repository this system came from. Nothing is
repository-local.

| Setting | Kind | What it is |
|---|---|---|
| `KITTY_CREDENTIALS_JSON` | org secret | the Kitty Bridge credential store |
| `KITTY_EGRESS_JSON` | org secret | the egress gateway, as kitty's own `egress.json`, in its versioned envelope |
| `KITTY_PROFILES_JSON` | org variable | the kitty profile(s): endpoint and model |

To enable the workflow on this repository:

1. Grant this repository access to the three settings above (organisation
   settings → Secrets and variables → Actions → each item → repository access).
2. Make sure this repository can use the self-hosted runner group carrying the
   `cap-nano` and `noble` labels.
3. Open a pull request. The `review` check appears on it.
4. Once it has run green once, make `review` a required status check on `main`.

### The egress guarantee

**The reviewer reaches the model only through the configured egress gateway.**
That is not a default that can be left off — five separate things enforce it, and
`rules/ci.md` describes each one and why neither of the two redundant-looking
checks subsumes the other. If the gateway does not resolve, **no review is
attempted and the check fails**; it never quietly falls back to the runner's own
network path.

### If the check goes red

The pull request gets a comment saying what happened, and the run summary carries
a per-attempt table. The two outcomes mean different things:

- **`exhausted`** — the provider could not serve the request (quota, credentials,
  a transient error). Nothing is wrong with the change. Top up or wait, then
  re-run. The job already retried once by itself.
- **`fatal`** — the workflow or its settings. The diagnostic names which. The
  most common causes are one of the three `KITTY_*` settings missing or not valid
  JSON, and an egress document that parses but disables the gateway.

One case is worth knowing in advance because it is easy to misdiagnose: **if the
job starts failing at `Install kitty-bridge and Claude CLI`, that is the runner
tier, not the settings.** It is reported as `fatal` and points at the `KITTY_*`
settings, which will all be fine. Move the job from `cap-nano` to `cap-light`.

---

## Changing the review system

`.github/workflows/ci.yml` runs `tests/test_review_scripts.py` on every pull
request — 492 tests over the selector, the classifier, the notices, the redactor,
the schema and the workflow's own wiring. Run them locally the same way:

```bash
python .github/review/tests/test_review_scripts.py
```

Not through `unittest discover`: discovery imports the start directory as a
package and `.github` is not a valid package name. The suite runs on a bare
interpreter with no installed dependencies, on purpose — a broken review workflow
has to be diagnosable without provisioning anything first — so do not add a test
there that imports a third-party package.

**Adding a source directory means adding it to `scripts/select_rules.py`.** A
path no pattern matches is reviewed with **zero rule files loaded**, and nothing
goes red to say so; the review just gets quietly shallower.

Every script here except `select_rules.py` is carried verbatim from the
repository this system came from, so fixes stay portable in both directions.
Changing one is a fork — say why, and consider upstreaming it.
