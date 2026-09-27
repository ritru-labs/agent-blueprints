# Phase 1C through 1F PostgreSQL control-plane experiments

This synthetic workflow stores task identity, policy, candidate provenance, verifier results, repair-input intent, session lineage, state transitions, and operation intents in PostgreSQL. The model and Agents API session are context, not the source of operational truth. `ports.py` defines candidate-source and trusted-verifier interfaces. Phase 1C uses the saved Phase 1A ZIP; Phase 1D adds a real two-turn Agents API adapter. Phase 1E published a scoped live GitHub draft PR. Phase 1F adds exact-head CI/review observation and same-PR repair history. Jira and automatic merge are not implemented.

## Boundaries

- `policy.json` is trusted, versioned configuration. Its canonical SHA-256 and JSON snapshot are stored with each run; recovery refuses a policy mismatch. It fixes the repository, base commit, verifier image, network mode, and the sole permitted synthetic operation kind. Agent output cannot select these values.
- The Phase 1A ZIP is checked against its checked-in digest, copied once to a private content-addressed store, and reconstructed with Phase 1B's exact three-file intake logic. Recovery rechecks the stored bytes and reconstructed tree hash before running candidate code or relying on an earlier `VERIFIED` state. Later corruption moves the run to `NEEDS_HUMAN` while retaining the historical verification record.
- Candidate and verification rows are append-only. A composite foreign key binds each verification to the same run, candidate, ZIP digest, tree digest, and base commit. A separate foreign key binds the run's current verification to its current candidate. PostgreSQL triggers enforce legal state changes and require a linked `PASS` row before `VERIFIED`.
- Schema version 2 preserves every candidate and verification. Candidate ordinals, source session/turn/artifact IDs, and repair-attempt links make the old `FAIL` and new `PASS` independently inspectable. The repair limit comes from the immutable policy snapshot and is checked in both application code and PostgreSQL. A replacement session, if explicitly used, has a predecessor row; the live proof continued the primary session.
- The repair-input row stores an idempotency key and SHA-256 of a deterministic, sanitized message, never its text. It is committed before API submission. The worker marks submission `UNCERTAIN` before the call. After a crash, the adapter reads saved session items, matches the message hash and turn, and records that observation. If no saved message can be established, it leaves the outcome uncertain and never blindly resends. A still-`PLANNED` input is reconciled before its first submission.
- The controller holds a PostgreSQL advisory lock for a run while recovering it. The lock disappears if the worker process dies. Each state change and its event commit together. Verification runs outside a database transaction in a separate constrained Docker container; its sanitized result is then committed with the state transition.
- Phase 1C external-operation intents have a unique stable key and payload digest. The only enabled kind there is `synthetic_notice`, which records a local result without sending anything. A repeated key with the same payload returns the same record; a changed payload is rejected. `OUTCOME_UNKNOWN` cannot be marked successful without reconciliation. Later GitHub adapters have separate scoped intents and credential isolation; no Jira adapter is present.
- Schema version 3 adds a separate publication intent linked to the current `PASS` verification and candidate. The trusted publisher reconstructs the allowlisted files from the stored ZIP and pinned base commit in an isolated temporary Git object store. It calculates the full Git tree and deterministic commit, then persists their SHA-1 IDs, target branch, local remote identity, and publisher config hash before pushing. The branch is derived from the run UUID, never model output. A create-only Git push is preceded by `OUTCOME_UNKNOWN`; after a crash, a fresh process confirms the exact remote ref and tree before marking `CONFIRMED`. An uncertain write with no observed ref remains unresolved without an automatic resend.
- Schema version 4 adds explicit `github` publication scope and immutable draft PR intent. A version 2 policy pins the GitHub actor and exact target base ref. The trusted GitHub adapter rechecks that base, the published head, and the Git tree; the draft PR adapter reconciles saved PRs by exact head/base/content before confirming an uncertain create. The scoped live Phase 1E proof published one branch and [Draft PR #1](https://github.com/ritru-labs/agent-blueprints/pull/1).
- Schema versions 5 through 9 add immutable PR head history, exact-head CI/review batches, a pinned observation policy, durable CI repair input, multiple immutable publications on one PR branch, and a readback-checked PR body refresh. The trusted observer requires the current PR, branch, base commit, and published tree to agree before and after reading checks and reviews. It stores bounded check/review metadata and allowlisted findings; raw CI logs and review prose never become agent instructions. A deferred database trigger refuses `PASS` without exactly one successful record for each required check and enough latest-review approvals on the current head. Unchanged repeated reads reuse one batch; a return to a previously seen result after a different result creates a new immutable batch so the latest gate stays authoritative. A repaired candidate needs fresh independent verification before the publisher can update the same draft PR branch with compare-and-swap push semantics. The body refresh names the current candidate and verification while retaining previous history in PostgreSQL.

## Run the proof

Use Python 3.12, PostgreSQL 17 command-line tools (`initdb`, `pg_ctl`), Docker, the pinned local Python image from Phase 1B, and the locally downloaded gitignored Phase 1A ZIP. The test creates a disposable PostgreSQL cluster with a private Unix socket and no TCP listener. It intentionally kills a child process after candidate commit, deletes its input copy, and launches a fresh child process to resume. It then checks exact hashes, one candidate and verification, repeated-resume idempotence, operation intent behavior, and tampering both before and after verification. Missing prerequisites fail the suite rather than reporting skipped tests.

```bash
cd solutions/jira-to-pr/experiments/control-plane
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s test -v
```

For a separately managed PostgreSQL database, set `PHASE1C_DATABASE_URL` in the trusted shell and run `cli.py init-db`, then `cli.py start --task DEMO-101 --interrupt-after-candidate` and `cli.py resume --task DEMO-101` in a new process. Use the same `--store-dir` on both invocations. The intentional first exit code is `75`. `cli.py inspect --task DEMO-101` prints sanitized state. Do not point this schema experiment at a shared database without reviewing the migration and privileges.

The Phase 1C integration test writes sanitized local evidence to gitignored `.control-runs/last-result.json`. The checked-in result is [`evidence/phase-1c-recovery.json`](evidence/phase-1c-recovery.json), with details in [`PHASE-1C-RESULTS.md`](PHASE-1C-RESULTS.md).

## Run the integrated Phase 1D live proof

The driver creates a disposable PostgreSQL 17 cluster with a private Unix socket, starts one Agents API session, and deliberately kills the first Node process after submitting its durable repair intent. A fresh Node process reconciles the saved message and turn, downloads Candidate #2, and invokes the independent Phase 1B Docker verifier. A third resume checks idempotence. The local API key remains in the ignored `../agents-api-spike/.env.local`; the driver does not print it or pass it to the verifier.

```bash
cd solutions/jira-to-pr/experiments/control-plane
.venv/bin/python -m unittest discover -s test -v
.venv/bin/python run_live_repair.py
```

The live command consumes API and hosted sandbox usage. It writes ignored `.control-runs/phase-1d-live-result.json`; the curated checked-in record is [`evidence/phase-1d-live-repair.json`](evidence/phase-1d-live-repair.json). [`PHASE-1D-RESULTS.md`](PHASE-1D-RESULTS.md) explains the proof and limitations. `repair_cli.py` is the trusted local bridge used by the Node adapter; its `history` command emits only sanitized database fields.

## Run the Phase 1E local publisher proof

This uses the downloaded Phase 1A artifact, the real Phase 1B Docker verifier, a disposable PostgreSQL cluster, and a disposable bare Git remote cloned from this repository. It pushes an isolated branch to that local remote, exits the first publisher process immediately after the push, and starts a fresh process to reconcile the exact remote commit and tree. It consumes no OpenAI API usage and requires no GitHub token.

```bash
cd solutions/jira-to-pr/experiments/control-plane
.venv/bin/python run_local_publication.py
```

The checked-in sanitized record is [`evidence/phase-1e-local-publication.json`](evidence/phase-1e-local-publication.json). [`PHASE-1E-RESULTS.md`](PHASE-1E-RESULTS.md) states what this local proof establishes and what remains for an actual GitHub draft PR.

`github_cli.py` is the explicit real GitHub entrypoint for `publish` and `draft-pr`. It requires a version 2 trusted policy file and a PostgreSQL run verified against that policy's exact base commit. It has no default write policy. The base branch must still point to that commit at publication and PR creation. Scoped live runs are retained under ignored `.control-runs/` directories; the [GitHub boundary ADR](../../architecture/ADR-0005-phase-1e-github-boundary.md) records the original publication conditions.

`run_github_draft.py` can create one qualifying run with a fresh Docker verification and a target base pinned to its current remote SHA. It requires `--target-base-ref` and the explicit `--execute-github-writes` flag. It keeps its PostgreSQL data, policy, and artifact store under ignored `.control-runs/github-live/` for later recovery. After an interrupted run, invoke it again with `--execute-github-writes` and no target-base argument; it will reconcile the saved operation instead of creating a new one. The script creates a real public workflow branch and draft PR, so review its target base and policy before invoking it.

## Phase 1F qualification

`observe_pr.py` reads an existing durable draft PR and records only sanitized exact-head check and review observations. `run_phase1f_qualification.py` is the staged live same-session driver. If the initial process dies after registering a session, rerunning `initial` reconciles its saved root turn and artifact without creating another session. A failed or ambiguous saved turn stops without resending input; start a fresh unique `--run-name` for a terminal failure and retain the old PostgreSQL history. The final evidence stage reads GitHub again and requires the latest exact-head gate, same-session lineage, and both restart markers before writing `PASS`. `run_phase1f_github_synthetic.py` uses clearly labelled local synthetic candidates to qualify the real GitHub CI and branch-update path while the Agents API organization limit is active. Its write stages require `--execute-github-writes`. Both drivers keep databases, ZIPs, policy files, and any raw process output under ignored `.control-runs/` directories. The checked-in [Phase 1F results](PHASE-1F-RESULTS.md) and evidence files contain only bounded operational metadata. Phase 1F remains open until the live Agents API repair continuation passes.

## Limits

These are scoped local and GitHub qualification proofs, not a production service rollout. The local content-addressed store detects tampering on read but is not a replicated or signed artifact service. Schema version 9 has been tested on disposable databases and migrated in the retained local synthetic proof database, not rolled out to an operated service. A crash during read-only verification may cause verification to run again. An uncertain repair submission with no saved API message stays unresolved for human reconciliation. Replacement-session operation is represented and tested in PostgreSQL but was not exercised with a live replacement sandbox. The Phase 1F GitHub CI and branch update proof uses a synthetic local repair source; the live Agents API continuation remains blocked by the organization's usage or billing limit. PostgreSQL access control, backups, and multi-host operation remain unqualified.
