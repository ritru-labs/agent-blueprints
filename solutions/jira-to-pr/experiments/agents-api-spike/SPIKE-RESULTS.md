# Phase 1A Agents API spike results

## Environment

- Live run: 2026-09-26 (Asia/Kolkata); reconciled by 2026-09-26 17:46 UTC.
- Local Python: 3.9.6; installed Python `openai` SDK: 2.48.0.
- Local Node: 26.8.2; pinned official JavaScript `openai` SDK: 7.23.0.
- Agents API: official SDK `client.beta.agents.sessions.create({ ..., stream: true })`; SDK sends `OpenAI-Beta: agents=v1` on session and artifact requests. No raw HTTP or Agents SDK was used.
- Saved session reported `environment.type=openai_hosted`, `environment.network.access=disabled`, and status `idle` after the root turn.
- One explicit live session was created. `reconcile-spike.mjs` made read-only requests against that session and started no new turn.

## Capabilities tested

| Capability | Result | Evidence |
| --- | --- | --- |
| Session creation | PASS | Live `agent.session.created`; session ID below. |
| Turn execution | PASS | Saved root turn status `completed`; root completion event received. |
| Native subagent | PASS | Native `agent.session.subagent.created`; saved subagent turn `completed` with a completed assistant message. One unique subagent ID. |
| Hosted sandbox | PASS | `agent.session.environment.connected`; saved environment type `openai_hosted`, network `disabled`. |
| File modification | PASS | Downloaded hosted output has changed `sample/app.py` and `sample/tests/test_app.py`, including `add(a, b)` and its test. |
| Command execution | PASS | Saved root `unittest` command item exited `0`; 2 tests passed. ZIP creation command item exited `0`. |
| Artifact creation | PASS | Artifacts API listed `/workspace/outputs/sample-project.zip` for the completed root turn. |
| Artifact retrieval | PASS | Trusted local program downloaded the 743-byte ZIP; validator accepted the three expected files and SHA-256 below. |
| Event visibility | PASS | Live stream reported session, root turn, subagent creation, environment connection, command items, and root completion. Subagent turn completion was verified through saved records because it was absent from this stream. |

## Evidence

- Session: `sess_0bc7de7fdb27ee0d006ab80385ab7c819bbeb057833aa8aa79`.
- Root turn: `turn_0bc7de7fdb27ee0d006ab8038d3d50819b922e967c00584325`, saved status `completed`, `subagent_id=null`. Root completion event: `evt_2d992636185a4d848d8fcb942844ccf16ef0ad9fc2564396ae`.
- Subagent: `subagent_1e20c651d5c6503b478d500978832b4e`. Saved turn: `turn_c6500b5c1b7a1cd61850cbf8c9c9d195`, status `completed`. Completed assistant message item: `msg_034377e8893c8411016ab80396e34487d0ad425f5ad09627c4`. The stream emitted two creation events for this same ID, not two distinct subagents.
- Hosted environment connected event: `evt_cbed775a3a124ca8a74364ec66e4235df73b3ef6b0b9425cad`. A read-only session retrieval confirmed `openai_hosted` and network `disabled`.
- Test command item: `exec_e092bea91b1b5da1a81654593a38d96ebf9c8bb3f04a437625`, exit `0`. Saved output excerpt: `test_add ... ok`, `test_identity ... ok`, `Ran 2 tests`, `OK`.
- The agent's first `rg` command exited `127` because `rg` was unavailable in the hosted sandbox. It continued using available tools. The subsequent test and ZIP creation command items both exited `0`.
- Artifact: `artifact_ea210af6c68d411fa36a4c6544b5e928b53b4a181c944983b1`, path `/workspace/outputs/sample-project.zip`, root turn ID matched. Downloaded ZIP: 743 bytes; SHA-256 `28ac19606a8ffa3e62833132107110abac1bc33ff621cf1de7926e1d07d30dda`. The local validator rejected unsafe paths and unexpected files, checked ZIP CRC, and confirmed the source and test differed from the supplied originals. It did not execute downloaded code.
- Local checks after the runner fix: `npm test` passed 3 Node tests and 3 Python validator tests. The original synthetic project passed its 1 local test. These are separate from the hosted 2-test result.
- Local detailed metadata and the downloaded ZIP are gitignored under `.spike-runs/`. The API key stayed in the gitignored, owner-only `.env.local` and was not passed into the sandbox or published evidence.
- The checked-in `evidence/phase-1a-live-run.json` contains the safe IDs, statuses, test exit code, artifact path, size, and SHA-256 in machine-readable form. It omits prompts, reasoning, raw logs, and credentials; reviewers can compare the IDs with saved API records.

## Differences from ADR and limitations

- The installed Python `openai` 2.48.0 lacked `client.beta.agents`, although the [official Agents API examples](https://developers.openai.com/api/docs/guides/agents-api/sessions) show that namespace. The spike used the official JavaScript SDK 7.23.0, which exposed the documented methods.
- The initial local runner reported failure after the root turn because it required a streamed subagent completion event. The stream showed creation and coordination, but no subagent completion event. The [multi-agent guide](https://developers.openai.com/api/docs/guides/agents-api/multi-agent) points to saved turns and items for subagent history. Read-only reconciliation confirmed a completed subagent turn and message, then retrieved the already published artifact. The runner now checks saved records before reporting success; it did not start another session or turn.
- **Reconciliation rule:** stream events are for progress and UX; saved session, turn, and item state is authoritative for completion and recovery. A missing or duplicate intermediate stream event cannot by itself prove failure or success.
- The subagent's session resource still reported `active` after its turn completed. The decision uses the completed saved turn and assistant message as execution evidence, not the subagent resource status alone.
- The saved items did not expose a discrete file-write item. The downloaded source differs from the supplied input, the root turn ran tests and packaged it, and the subagent's saved tool items were resource-listing calls. This supports the intended division of work but does not prove which agent made each filesystem write.
- The requested analysis-only subagent role was prompted and one subagent ran, but this spike did not enforce a technical read-only filesystem boundary between agents sharing the hosted workspace.
- This phase did not test same-session continuation, sandbox-expiry recovery, independent verification of downloaded code, Jira integration, GitHub publication, or draft PR creation. The hosted test is not a trusted external verifier.

## Decision

**PROCEED WITH CHANGES**, subject to architecture review. The live Agents API primitives required for Phase 1A worked in one synthetic run. Future work should keep the official JavaScript SDK (or wait for a Python SDK with the namespace), reconcile subagent execution through saved turns/items, treat duplicate or absent intermediate stream events as possible, and keep privileged application functions outside subagents. This result permits consideration of the next phase; it does not establish production readiness or authorize Jira/GitHub integration before review.
