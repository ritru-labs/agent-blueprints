# Phase 1C PostgreSQL control-plane experiment

This synthetic workflow stores task identity, policy, candidate provenance, verifier results, state transitions, and operation intents in PostgreSQL. The model and Agents API session are context, not the source of operational truth. `ports.py` defines candidate-source and trusted-verifier interfaces. The local adapters consume the existing Phase 1A downloaded ZIP and call the existing Phase 1B Docker verifier; they do not create a new Agents API turn or contact Jira or GitHub.

## Boundaries

- `policy.json` is trusted, versioned configuration. Its canonical SHA-256 and JSON snapshot are stored with each run; recovery refuses a policy mismatch. It fixes the repository, base commit, verifier image, network mode, and the sole permitted synthetic operation kind. Agent output cannot select these values.
- The Phase 1A ZIP is checked against its checked-in digest, copied once to a private content-addressed store, and reconstructed with Phase 1B's exact three-file intake logic. Recovery rechecks the stored bytes and reconstructed tree hash before running candidate code or relying on an earlier `VERIFIED` state. Later corruption moves the run to `NEEDS_HUMAN` while retaining the historical verification record.
- Candidate and verification rows are append-only. A composite foreign key binds each verification to the same run, candidate, ZIP digest, tree digest, and base commit. A separate foreign key binds the run's current verification to its current candidate. PostgreSQL triggers enforce legal state changes and require a linked `PASS` row before `VERIFIED`.
- The controller holds a PostgreSQL advisory lock for a run while recovering it. The lock disappears if the worker process dies. Each state change and its event commit together. Verification runs outside a database transaction in a separate constrained Docker container; its sanitized result is then committed with the state transition.
- External-operation intents have a unique stable key and payload digest. The only enabled kind is `synthetic_notice`, which records a local result without sending anything. A repeated key with the same payload returns the same record; a changed payload is rejected. `OUTCOME_UNKNOWN` cannot be marked successful without reconciliation. No Jira or GitHub adapter or write credential is present.

## Run the proof

Use Python 3.12, PostgreSQL 17 command-line tools (`initdb`, `pg_ctl`), Docker, the pinned local Python image from Phase 1B, and the locally downloaded gitignored Phase 1A ZIP. The test creates a disposable PostgreSQL cluster with a private Unix socket and no TCP listener. It intentionally kills a child process after candidate commit, deletes its input copy, and launches a fresh child process to resume. It then checks exact hashes, one candidate and verification, repeated-resume idempotence, operation intent behavior, and tampering both before and after verification. Missing prerequisites fail the suite rather than reporting skipped tests.

```bash
cd solutions/jira-to-pr/experiments/control-plane
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s test -v
```

For a separately managed PostgreSQL database, set `PHASE1C_DATABASE_URL` in the trusted shell and run `cli.py init-db`, then `cli.py start --task DEMO-101 --interrupt-after-candidate` and `cli.py resume --task DEMO-101` in a new process. Use the same `--store-dir` on both invocations. The intentional first exit code is `75`. `cli.py inspect --task DEMO-101` prints sanitized state. Do not point this schema experiment at a shared database without reviewing the migration and privileges.

The integration test writes sanitized local evidence to gitignored `.control-runs/last-result.json`. The example checked-in record is [`evidence/phase-1c-recovery.json`](evidence/phase-1c-recovery.json), and the outcome is described in [`PHASE-1C-RESULTS.md`](PHASE-1C-RESULTS.md).

## Limits

This is a local synthetic persistence and recovery proof. The test destroys its PostgreSQL cluster afterward. The local content-addressed store detects tampering on read but is not a replicated or signed artifact service. PostgreSQL access control, backups, migrations beyond version 1, multi-host worker coordination, real repair loops, review gates, actual external-action reconciliation, and production operations still need design and qualification. The policy records a repair limit but this phase does not execute repairs. A crash during the read-only verifier may cause that verification to run again; the demonstrated interruption occurs after candidate creation, and repeated recovery creates no duplicate candidate, verification, event, or operation record.
