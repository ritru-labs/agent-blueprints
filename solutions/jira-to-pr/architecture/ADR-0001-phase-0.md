# ADR 0001: Jira-to-draft-PR blueprint architecture

- Status: Proposed for review
- Date: 2026-09-26
- Scope: Phase 0 design only; no application or live workflow has been implemented

## Context and repository baseline

The public `ritru-labs/agent-blueprints` repository is empty. The local `main` has no commits or tracked files, and GitHub reports repository size `0`. There is no demo application, test suite, base commit, or runnable workflow to qualify yet. The configured local Git identity is `Binnu Kyadari <binnukyadarirh97@gmail.com>`.

The first blueprint should show a real Agents API session delegating bounded work, changing a synthetic demo API, executing tests, and producing evidence for a draft PR. Jira and GitHub may initially use clearly labeled mock or dry-run adapters. A dry-run PR request is not an actual GitHub PR.

The repository needs a maintainer-owned bootstrap commit on `main` containing the initial demo app, with the invoice-download endpoint deliberately absent. That one-time bootstrap is separate from task delivery. Subsequent agent tasks start from an immutable base commit on a dedicated branch; they never push to `main`. Until the bootstrap exists, a Jira-to-PR demonstration cannot run.

## Decision

Use the **OpenAI Agents API**, not the separately documented Agents SDK, for the managed Codex harness and durable session. The official Python examples use `client.beta.agents.sessions.create(...)`; raw HTTP examples use `OpenAI-Beta: agents=v1`. Phase 1 must pin and test an SDK version containing these beta methods rather than copying an older SDK example. [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview), [configuring agents](https://developers.openai.com/api/docs/guides/agents-api/configuration).

```text
Mock or Jira ticket -> trusted application -> Agents API session
                       |                    -> main agent + optional subagents
                       |                    -> isolated coding sandbox
                       -> trusted artifact intake and verifier
                       -> verified branch publisher -> dry-run or draft PR
                       -> mock or Jira status update
Human reviewer -> merge and deployment decisions
```

The application owns task identity, policy, workflow state, credentials, verification, publication, and recovery. The agent owns coding decisions inside its sandbox and can recommend actions. Its statements and tool arguments do not satisfy gates by themselves.

### Agent and delegation

Create one session per delivery task and persist its ID beside the ticket ID, repository, base commit, task branch, state, checkpoint artifact, candidate tree hash, verification results, external action IDs, and timestamps. Continue the same session for related repairs while the session is usable. Set `agent.multi_agent.enabled=true` and `max_concurrent_subagents=2` initially. The coordinator decides whether to delegate independent repository analysis, test analysis, or post-change review. Subagents share the same filesystem, so analysis and review are read-only; the coordinator alone edits candidate code. A subagent review is an additional perspective, not an independent security guarantee. [Run and continue sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions), [multi-agent](https://developers.openai.com/api/docs/guides/agents-api/multi-agent).

### Environment and code handoff

Start with an OpenAI-hosted sandbox for the small synthetic demo, subject to a Phase 1 feasibility spike. The trusted application prepares an archive from the pinned base commit and supplies it to the sandbox. The agent edits and runs exploratory commands there without OpenAI, GitHub, or Jira application credentials. At a completed turn, the candidate source is exported under `/workspace/outputs` and downloaded as a session artifact. The trusted application treats that archive as untrusted: limit size and paths, reject traversal, symlinks, unexpected file types or paths, and changes outside the allowed project; reconstruct a clean candidate tree from the pinned base and accepted files. A separate disposable verifier with no publication credentials runs the configured checks on that exact tree. When real GitHub integration is enabled, only a trusted publisher receives GitHub write credentials, creates the branch commit from the verified tree, pushes it, confirms the remote SHA, and creates a draft PR. Any content change after verification invalidates the gate. [OpenAI-hosted sandboxes](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted), [files and artifacts](https://developers.openai.com/api/docs/guides/agents-api/environments/files), [sandbox security](https://developers.openai.com/api/docs/guides/agents-api/environments/security).

The sandbox network policy is explicitly `disabled` during the first spike if dependencies can be provisioned ahead of execution. If that is infeasible, use `restricted` access with every required exact host documented and tested. Hosted sandbox networking otherwise defaults to enabled. The sandbox gets only synthetic data. A self-hosted isolated executor remains a fallback if archive transfer, package provisioning, or verification cannot meet the required boundary; changing environment type requires an ADR update. [OpenAI-hosted sandbox network controls](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted).

The Agents API session and hosted filesystem have different lifetimes. A hosted sandbox may be deleted after keep-alives stop for an hour. Persist each accepted candidate/checkpoint and the application state outside it. On restart, retrieve the session and pending required actions, reconcile external writes, and check whether the environment and checkpoint are available. Resume the same session only where the environment and workflow can be safely restored; otherwise mark the task `NEEDS_HUMAN` or start a replacement session explicitly linked to the prior one. Do not claim same-session workspace recovery until tested. [Manage sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions/manage), [hosted sandbox lifetime](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted).

### Function tools and credentials

Expose narrow application-run tools for ticket lookup and, once enabled, ticket update and draft PR creation. The application validates every call against the stored task record: allowed ticket, repository, base branch, verified branch and commit, status transition, payload length, and caller authorization. Do not let agent-supplied repository or branch names override that record. The branch publisher is an internal trusted workflow step, not an arbitrary agent shell or GitHub-command tool. Keep OpenAI, GitHub, and Jira application credentials outside coding and verification environments. OpenAI Vaults support placeholder credentials for restricted hosted-sandbox HTTP requests, but this first blueprint does not need that path for privileged writes. [Function tools](https://developers.openai.com/api/docs/guides/agents-api/tools/functions), [sandbox security](https://developers.openai.com/api/docs/guides/agents-api/environments/security), [vaults](https://developers.openai.com/api/docs/guides/agents-api/tools/vaults).

External writes must be idempotent where possible. Persist the intended PR or ticket update before execution, use a stable operation key, and reconcile GitHub/Jira read-only after a timeout or process crash before retrying. A function-call item in history is not proof that a function is pending; use the session's `required_actions`, and return results with its `turn_id` and `call_id`. [Function tools](https://developers.openai.com/api/docs/guides/agents-api/tools/functions), [manage sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions/manage).

### Deterministic workflow and evidence

Application state is a typed enum. The initial workflow needs `RECEIVED`, `RUNNING`, `VERIFYING`, `REVIEWING`, `REPAIRING`, `READY_FOR_DRAFT`, `DRAFT_CREATED`, `NEEDS_HUMAN`, and `FAILED`. Define legal transitions in application code; only that code may change state. Permit at most two repair attempts. A failed or interrupted verification cannot transition to `READY_FOR_DRAFT`.

The trusted verifier runs an allowlisted, repository-configured command set on a clean candidate tree: format/lint, unit tests, cross-tenant authorization tests, demo API integration test, and secret/path checks. Each result records the command, exit code, timestamp, bounded sanitized output, base commit, candidate tree hash, and environment identity. A real disposable demo app with synthetic tenants must serve the integration test. The review step records file-specific findings; any blocking finding requires repair and a fresh verification and review of the resulting tree. The PR gate requires a matching tree hash, passing mandatory checks, completed review, no unresolved blocking finding, valid branch scope, and a clean publication tree. The human still owns merge and deployment.

Jira ticket text and repository content are untrusted inputs, including possible prompt injection. Instructions in either cannot expand tool permissions or change deterministic gates. Running candidate code in a verifier is itself a risk, so the verifier must have no production credentials and limited network/filesystem access. [Sandbox security](https://developers.openai.com/api/docs/guides/agents-api/environments/security).

### Events, observability, and async path

The CLI follows session events and identifies root-turn completion, failure, and cancellation; a closed stream or idle status alone is not success. On `agent.session.requires_action`, retrieve the session and handle each pending action. Render subagent creation and coordination only from recorded events/items, never from planned roles. Save session ID, turn IDs, item/event IDs where provided, bounded event summaries, command evidence, retry count, and final state. The Platform Agents logs and traces support inspection by session ID. A later worker can consume signed session webhooks; the CLI version does not need a public endpoint. Avoid recording secrets, raw ticket data, or hidden reasoning in published evidence. [Events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events), [multi-agent observation](https://developers.openai.com/api/docs/guides/agents-api/multi-agent), [session webhooks](https://developers.openai.com/api/docs/guides/agents-api/sessions/webhooks), [observability](https://developers.openai.com/api/docs/guides/agents-api/observability), [tracing](https://developers.openai.com/api/docs/guides/agents-api/tracing).

## Capability and claim check

| Prompt claim | Phase 0 finding | Source |
| --- | --- | --- |
| Managed Codex harness and durable session | Documented; session continuity does not guarantee persistent sandbox files. | [Overview](https://developers.openai.com/api/docs/guides/agents-api/overview), [hosted sandbox](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted) |
| Native dynamic subagents and concurrency limit | Documented; shared filesystem requires edit coordination. | [Multi-agent](https://developers.openai.com/api/docs/guides/agents-api/multi-agent) |
| OpenAI-hosted code execution and artifacts | Documented; artifact transfer into a trusted Git publication path is application work. | [Hosted sandbox](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted), [files](https://developers.openai.com/api/docs/guides/agents-api/environments/files) |
| Application-side function calls and required actions | Documented; app must validate arguments and return the result to the same session. | [Functions](https://developers.openai.com/api/docs/guides/agents-api/tools/functions) |
| Vault-backed sandbox HTTP credentials | Documented but unnecessary for v0.1 privileged writes. | [Vaults](https://developers.openai.com/api/docs/guides/agents-api/tools/vaults) |
| Stream events, saved items, subagent events, webhooks, traces | Documented; no live event behavior has been verified in this repository. | [Events](https://developers.openai.com/api/docs/guides/agents-api/sessions/events), [webhooks](https://developers.openai.com/api/docs/guides/agents-api/sessions/webhooks), [observability](https://developers.openai.com/api/docs/guides/agents-api/observability) |
| Hosted sandbox deny-by-default network | Must be configured; default access is enabled. | [Hosted sandbox](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted) |
| Real Jira update and real draft PR in first run | Not established by the prompt's mock/dry-run adapters. Must be separate acceptance levels. | Application design decision |

## Delivery levels and Phase 1 entry criteria

**Offline demo:** mock ticket, simulated Agents API event fixtures only, real local code/test commands and synthetic API, dry-run PR request. Label all simulated parts. This is useful for adapter and gate tests, but it does not satisfy a real Agents API claim.

**Live Agents API demo (version 0.1 target):** real session, observed native subagent delegation, actual hosted sandbox edit, actual artifact transfer, trusted verification, mock ticket update, and dry-run PR request. Requires an opt-in API key and measured session/cost limits. The result may be called a live Agents API demo, not a real Jira-to-GitHub delivery.

**Integrated demo:** all live-demo conditions plus a confirmed pushed branch, actual GitHub draft PR, and actual Jira update through scoped application adapters. Merge and deployment stay human-controlled. External writes require explicit configuration and read-after-write reconciliation.

Before implementation, resolve the one-time `main` bootstrap, choose the exact archive and dependency provisioning method in a small live spike, verify SDK method names/version and environment behavior, and define allowed repository/ticket scope. No live OpenAI session, sandbox, Jira, or GitHub write was executed in Phase 0. The Agents API currently documents US-only data residency and no Zero Data Retention support, so the demo uses synthetic data and avoids client tickets. [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview).

## Prompt corrections before Phase 1

1. Replace "Then proceed to PHASE 1" with "Stop after the Phase 0 ADR and capability check for review." This resolves the pasted prompt's conflicting stop instruction.
2. Replace "draft PR created" in v0.1 acceptance with the chosen delivery level's exact outcome. A generated request is not a GitHub PR.
3. Add the artifact intake, trusted verification, branch publication, exact-tree gate, and recovery path above.
4. Require verification and review again after every repair, bound to the tree that is pushed.
5. Keep the original repository's Git identity and feature-branch rules for later commits, but handle the empty repository's one-time `main` bootstrap as a separate maintainer action.

This ADR records a documentation-backed design. It does not establish that the proposed end-to-end workflow works; that requires Phase 1 implementation and live evidence.
