# Snapshot tier

Sanitized captures of real pages, for whole-document behaviour. Empty on
purpose: every rule that governs this directory is proven to fire against
synthetic input in `tests/test_corpus_policy.py`, so the guard is armed without
this repository publishing a third-party page.

Adding one is a reviewed decision, not a convenience. Read section 3.3 of
`.system_design/TEST_SUITE.md` first: it lists the three triggers that justify a
snapshot, the sidecar fields a snapshot must carry, and what sanitization means
here. A page whose terms forbid redistribution does not belong in a public
repository at all.
