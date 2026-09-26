# Phase 1A Agents API spike results

## Environment

- Date: 2026-09-26 (Asia/Kolkata).
- Local Python: 3.9.6; installed Python `openai` SDK: 2.48.0.
- Local Node: 26.8.2; pinned official JavaScript `openai` SDK: 7.23.0.
- Agents API: documentation uses `client.beta.agents.sessions.create(...)`; the SDK's generated Agents requests send `OpenAI-Beta: agents=v1`. No raw HTTP calls are used. This beta behavior is inspected locally, not confirmed in a live request.
- Hosted environment requested by the spike: `openai_hosted` with `network.access=disabled`; no live environment has been created.

## Capabilities tested

`FAIL — not run` means the capability has no live evidence; it does not mean the API rejected it.

| Capability | Result | Evidence |
| --- | --- | --- |
| Session creation | FAIL — not run | `OPENAI_API_KEY` absent from this execution environment. |
| Turn execution | FAIL — not run | No turn ID. |
| Native subagent | FAIL — not run | No `agent.session.subagent.created` event or subagent turn. |
| Hosted sandbox | FAIL — not run | No environment ID or connected event. |
| File modification | FAIL — not run | No hosted file change observed. |
| Command execution | FAIL — not run | No hosted command item or exit code. |
| Artifact creation | FAIL — not run | No completed turn or published artifact. |
| Artifact retrieval | FAIL — not run | No artifact ID, bytes, or hash. |
| Event visibility | FAIL — not run | No live session event stream. |

## Evidence

- The installed Python SDK 2.48.0 lacks `OpenAI(...).beta.agents` (`hasattr` returned `False`). The pinned JavaScript SDK 7.23.0 exposes session creation and artifact list/content methods (`function function function` in local inspection).
- `npm test`: 3 Node configuration/event/outcome tests and 3 Python archive validation tests passed locally. The synthetic project's original `unittest` passed (1 test). These offline tests are not Agents API evidence.
- `npm run spike` without a key exited 1 with: `OPENAI_API_KEY is missing. Set it locally before explicitly running npm run spike.` No API request was made. The key's value was never printed.
- No session, turn, subagent, environment, item, or artifact identifiers exist from this run. No live trace exists to inspect.

## Differences from ADR

- The ADR cites official Python examples using `client.beta.agents`, but the installed and publicly available Python `openai` 2.48.0 did not expose that namespace on 2026-09-26. This spike uses the official JavaScript SDK 7.23.0. It does not silently substitute the Agents SDK.
- The [current Agents API multi-agent documentation](https://developers.openai.com/api/docs/guides/agents-api/multi-agent) explicitly says subagents do not support function tools. ADR-0001 was accepted with this constraint before the spike.
- Session continuation is documented through [`sessions.events.create`](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) on an existing session. It was checked in documentation but not exercised. The Phase 1A scenario needs only the first turn.
- The [hosted environment](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted), [events](https://developers.openai.com/api/docs/guides/agents-api/sessions/events), and [artifact](https://developers.openai.com/api/docs/guides/agents-api/environments/files) guides informed the request and evidence gates. Those behaviors remain unverified until a live run.

## Decision

**BLOCKED.** The explicit live run cannot start until `OPENAI_API_KEY` is available to the process running `npm run spike`. The offline checks prove only local utility behavior and SDK method presence. Run the command once with a local key, inspect `.spike-runs/last-run.json` and the downloaded ZIP, then update this file from observed evidence before deciding whether the architecture may proceed. No Jira or GitHub integration should follow from the current result.
