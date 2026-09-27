# ADR 0004: Exact-candidate Git publication through a trusted local adapter

- Status: Implemented and demonstrated for a disposable local bare remote; GitHub draft publication remains open
- Date: 2026-09-27
- Scope: Phase 1E local publisher qualification

## Decision

Schema version 3 adds an immutable `publication_attempts` intent bound by foreign keys to the current candidate and its independent `PASS` verification. PostgreSQL checks the run is `VERIFIED`, the candidate and verification IDs are current, the base commit matches, and the branch is derived from the run UUID. The publisher stores the candidate ZIP hash, verifier tree hash, full Git tree hash, deterministic commit hash, branch ref, local remote identity, and trusted publisher config hash before any push. Candidate, verification, and publication intent IDs cannot be changed afterward.

The trusted adapter opens the private content-addressed ZIP, rechecks its digest and the verifier's canonical tree hash, and reconstructs only the Phase 1B allowlisted files over the pinned base Git tree in a temporary Git object store. It requires changes to both source and tests and rejects changes outside that allowlist. It creates a commit with the pinned base as its sole parent and fixed publisher identity. The verification timestamp makes the commit deterministic across process restarts. Candidate text supplies no shell command, remote, branch, author, or PR content.

The adapter reads the remote ref, records `OUTCOME_UNKNOWN` before a create-only push, and confirms only after reading back the exact remote commit and Git tree. If the first process dies after pushing, the next process reconstructs the same intent and confirms the saved remote state. If the ref is absent after an uncertain write, it stops without a blind resend. A remote ref pointing to another commit is a conflict. A confirmed ref that later moves is detected on subsequent inspection.

This first adapter accepts only an explicitly supplied absolute, non-symlink local bare Git repository. The existing run policy still prohibits external writes. The local remote is disposable test infrastructure; it is not GitHub publication. The eventual GitHub adapter must add scoped credentials, repository allowlisting, read-after-write reconciliation against GitHub, an exact-head review gate, and durable draft PR intent and recovery before the workflow can claim Phase 1E completion.

## Evidence and limits

The [local Phase 1E result](../experiments/control-plane/PHASE-1E-RESULTS.md) includes a real Phase 1B Docker `PASS` for the Phase 1A artifact, a pushed commit to a disposable bare remote, an intentional publisher process death immediately after push, a fresh-process reconciliation, and a repeated resume with one durable publication row. Negative tests cover tampered ZIPs, changed remotes, remote branch collisions/drift, competing workers, uncertain absent refs, and mutation of publication intent.

The test uses a pre-existing saved Phase 1A candidate. It does not publish the Phase 1D repaired candidate, exercise a GitHub API, create a draft PR, satisfy an independent code review gate, or qualify an operated PostgreSQL/artifact service. Git SHA-1 identifies the Git tree and commit; the archive and canonical candidate tree retain their separate SHA-256 checks.
