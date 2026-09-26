# Phase 1A Agents API feasibility spike

This small experiment asks one native Agents API subagent for read-only test advice. The coordinator then edits a synthetic Python project in an OpenAI-hosted sandbox, runs `unittest`, and publishes a ZIP under `/workspace/outputs`. The local program downloads it and checks its paths, expected files, modifications, size, and SHA-256 without executing the downloaded code. Events are logged only when received from the API. The [Agents API multi-agent guide](https://developers.openai.com/api/docs/guides/agents-api/multi-agent) documents the native subagent events and states that subagents do not support function tools.

This experiment does not test Jira, GitHub writes, draft PR creation, independent candidate verification, recovery, or production readiness. A passing hosted test is not a separate trusted verifier.

## Run

The local Python `openai` package inspected on 2026-09-26 was 2.48.0 and did not expose `client.beta.agents`; the pinned official JavaScript SDK did. Node 26 and Python 3 are used here. The SDK sends the documented `OpenAI-Beta: agents=v1` header for these endpoints. See the [session guide](https://developers.openai.com/api/docs/guides/agents-api/sessions), [hosted environment guide](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted), and [artifact guide](https://developers.openai.com/api/docs/guides/agents-api/environments/files).

```bash
cd solutions/jira-to-pr/experiments/agents-api-spike
npm ci
npm test
(cd sample-project && python3 -m unittest discover -s sample/tests -v)
export OPENAI_API_KEY='set-this-in-your-shell'
npm run spike
```

If you keep the key in the gitignored, owner-readable `.env.local`, invoke `node --env-file=.env.local run-spike.mjs` instead. For a completed session whose stream ended before all intermediate evidence was recorded, use `node --env-file=.env.local reconcile-spike.mjs`. Reconciliation reads saved records and downloads the existing artifact; it does not create another turn.

Set `OPENAI_API_KEY` only in the trusted local shell. Do not put it in the sandbox, commit it, or paste it into logs. The live command consumes paid API/model and hosted sandbox usage; unit tests do not. Networking inside the hosted sandbox is disabled, and no third-party dependencies are needed there. The script fails clearly if the key or documented SDK methods are missing. It does not run on `npm install` or `npm test`.

Successful output should include actual session, environment, turn, subagent, command, and artifact records. The script reports success only when the root turn completes, a native subagent is observed and its completed turn and assistant message are found in saved records, a passing `unittest` command item is found, and a matching ZIP artifact is downloaded and validated. A closed event stream alone is treated as an error. Sanitized metadata is written to ignored `.spike-runs/last-run.json`; the downloaded ZIP is kept there for local inspection. Inspect server-side session logs and traces in the [OpenAI Platform Agents logs](https://developers.openai.com/api/docs/guides/agents-api/observability) using the session ID. The checked-in `SPIKE-RESULTS.md` records what actually occurred.

**Reconciliation rule:** stream events are for progress and UX. Saved session, turn, and item state is authoritative for completion and recovery. The live stream showed subagent creation but did not supply a reliable completion event; saved subagent turns/items established completion. The checked-in `evidence/phase-1a-live-run.json` contains only safe IDs, states, exit code, artifact path, size, and hash so a reviewer can compare it with saved API records. Raw logs, the downloaded ZIP, and the API key remain gitignored.

The API and SDK are beta and may change. The prompt asks the coordinator to keep the subagent read-only; this spike observes delegation and the resulting command events but is not a security boundary for what a subagent can do in a shared sandbox. The ZIP validator is deliberately small and only accepts the three expected synthetic Python files.
