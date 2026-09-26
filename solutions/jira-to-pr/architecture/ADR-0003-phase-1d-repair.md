# ADR 0003: Durable same-session repair after trusted verification failure

- Status: Implemented and demonstrated as a local synthetic Phase 1D-B proof
- Date: 2026-09-27
- Scope: PostgreSQL control plane and real Agents API repair turn; no Jira/GitHub workflow writes

## Decision

Schema version 2 extends the Phase 1C run rather than storing repair state in a sidecar JSON file. A run has an original session and an explicitly linked current session, immutable candidate rows numbered from 1, one immutable verification per candidate, and a repair-attempt row linked to the exact failed verification. The run points to its current candidate and verification; prior rows remain queryable. PostgreSQL foreign keys and transition guards reject stale verification reuse, candidate swapping, a repaired artifact from an unrelated turn, and a verified state without a `PASS` for the current candidate.

The controller commits Candidate #1 before independent verification. A repairable `FAIL` is committed with allowlisted findings. It enters `REPAIR_PENDING` only if the policy snapshot permits another attempt. A deterministic feedback message is derived from the sanitized finding. PostgreSQL stores its hash and idempotency key, not its text. The adapter marks the intent `UNCERTAIN` before submitting it to the Agents API. HTTP acceptance is not treated as durable local acknowledgement.

On restart, the adapter reads saved messages from the stored current session. It hashes their input text and accepts exactly one match for the planned input, then binds the saved message ID and turn ID to the repair attempt. If the matching turn is still active it waits; if no matching saved message exists it stops without resubmitting an uncertain input. Candidate #2 must be the artifact of that observed turn, differ in artifact and tree hashes, and pass a fresh independent verification. The model cannot set the repair budget or promote itself to `VERIFIED`.

An explicit replacement session has a predecessor link and updates the run's current session only during `REPAIR_PENDING`. This preserves earlier candidate, verification, and session history. The live proof used one primary session; a live replacement session and restoration of its sandbox inputs remain separate qualification work.

## Evidence and limits

The [Phase 1D result](../experiments/control-plane/PHASE-1D-RESULTS.md) includes a real first-process exit after API submission, a fresh-process saved-state reconciliation, distinct `FAIL` and `PASS` candidate/verification pairs, and an unchanged repeated resume. Real PostgreSQL negative tests cover budget, lineage, candidate and verifier links, policy drift, worker exclusion, and artifact tampering. The experiment uses a deliberately seeded defect and a disposable local database. It does not establish arbitrary-defect recovery, production database operations, Jira writes, GitHub publication, a draft PR, or merge/deployment readiness.
