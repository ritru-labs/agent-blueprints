# Phase 1C: durable control-plane and recovery result

**Result: PASS for a local synthetic workflow.** Five tests passed: three used PostgreSQL 17.11 and the existing Phase 1B Docker verifier; two rejected false `PASS` records with zero trusted tests or a different candidate tree. See [`evidence/phase-1c-recovery.json`](evidence/phase-1c-recovery.json) for sanitized IDs, hashes, and observed outcomes. This is a local proof, not an operated production service.

## Recovery proof

1. A new worker admitted the Phase 1A candidate, checked its ZIP digest, stored it by SHA-256, computed the Phase 1B tree hash, and committed `RECEIVED -> CANDIDATE_READY` to PostgreSQL.
2. The worker deliberately exited with code `75` immediately after that commit. PostgreSQL showed exactly one candidate, zero verifications, and two state events.
3. The test deleted the worker's input ZIP copy. A fresh Python process loaded the run, policy, and candidate reference from PostgreSQL, rechecked the stored archive and tree, then ran the independent verifier. It committed `VERIFYING -> VERIFIED` with one linked `PASS` verification. The candidate ZIP digest was `28ac19606a8ffa3e62833132107110abac1bc33ff621cf1de7926e1d07d30dda`; the tree digest was `27ce9688fd58e3eac336c87293a99be1d5410ca93d76744cb6c3dbd9bfbe64cc`.
4. Another fresh `resume` returned the same candidate and verification IDs. The database still contained one candidate, one verification, and four state events.

A competing worker was denied by the PostgreSQL advisory lock while recovery was in progress. Recovery with a different valid policy snapshot was rejected without changing the verified record.

The `synthetic_notice` operation used the same row for a repeated key and payload, rejected a changed payload, and blocked a transition from `OUTCOME_UNKNOWN` to success. This operation sent no message or external request.

## Failure proof

A separate interrupted run had its stored ZIP altered before restart. Recovery moved it to `NEEDS_HUMAN` with zero verification rows. Direct attempts to update candidate or verification rows, reverse an illegal workflow transition, and insert a verification with a forged candidate tree hash were rejected by PostgreSQL.

A third run reached `VERIFIED`, then its stored ZIP was changed. The next resume moved the run to `NEEDS_HUMAN`, retained its one historical verification row, and blocked a new synthetic operation. The earlier `PASS` remains evidence about the original bytes, not permission to use changed bytes.

## Scope

The Phase 1A session is represented by its saved session/artifact evidence and the downloaded ZIP. The Phase 1B verifier is invoked through a trusted adapter and still performs its own Docker checks. Phase 1C adds PostgreSQL state and recovery around those boundaries. It does not execute a new Agents API session, Jira call, GitHub publication, PR creation, real external operation, or repair cycle. The local PostgreSQL cluster was intentionally disposable; production database operations and multi-host recovery are unverified.
