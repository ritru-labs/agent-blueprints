# Phase 1D-B: durable integrated repair result

**Result: PASS for one local synthetic workflow.** The live proof used one primary Agents API session and a disposable PostgreSQL 17 cluster. [Sanitized evidence](evidence/phase-1d-live-repair.json) records one run, Candidate #1 `FAIL`, one repair attempt, Candidate #2 `PASS`, distinct artifact and tree hashes, two fresh verification rows, and final `VERIFIED`. Raw test output, prompts, reasoning, ZIPs, and API keys are absent from PostgreSQL and checked-in evidence.

## Crash-window proof

1. Candidate #1 was downloaded from its completed saved turn, committed to PostgreSQL with its session, turn, artifact, archive, and tree IDs, then independently verified in the Phase 1B restricted Docker container. The trusted requirement suite failed on an intentionally seeded `add(a, b) -> 0` implementation.
2. PostgreSQL committed the `FAIL` verification and fixed `ADD_ARITHMETIC` finding. It then committed a repair attempt with a deterministic idempotency key and input SHA-256. The worker marked the submission `UNCERTAIN` before calling `agent.session.input.message` in the same session.
3. The first Node process exited with code `75` immediately after the API accepted that input and before local acknowledgement. PostgreSQL held `REPAIR_INPUT_UNKNOWN`, exactly one candidate, one verification, and one repair attempt.
4. A fresh Node process read the saved session items, matched exactly one user message by the stored input hash, bound its message and turn IDs to the attempt, waited for the completed turn, and downloaded Candidate #2. PostgreSQL committed Candidate #2 separately and the trusted verifier ran from scratch, passing 5 trusted tests and 6 candidate tests.
5. A repeated resume made no durable changes. The final counts are two candidates, two verifications, one repair attempt, and one session.

The candidate source and independent verifier are tied by archive and clean-tree hashes; Candidate #1's `FAIL` cannot authorize Candidate #2. The only feedback delivered to the model was an allowlisted code and fixed requirement message. The failed candidate was intentionally constructed for this proof; arbitrary model-repair reliability is not measured.

## Negative gates

Thirteen control-plane tests passed on real disposable PostgreSQL databases. The new tests reject stale verification reuse, applying Candidate #1's verification to Candidate #2, candidate swapping or an unrelated repair turn, an unchanged tree, repair-budget bypass, a conflicting session, a mismatched saved message, policy drift, competing workers, and artifacts changed before, during, or after verification. They also exercise both allowed repair attempts through Candidate #3. The Node decision test verifies that an uncertain input with no saved message is not resent. Existing Phase 1A and Phase 1B offline suites still pass.

## Boundary

The control plane is now authoritative for the demonstrated repair lifecycle. The database and artifact store were deliberately disposable for the live proof; operational backup, replication, access control, multi-host recovery, and live replacement-session restoration remain unqualified. No Jira API, GitHub branch publisher, draft PR creation, or external workflow write was added.
