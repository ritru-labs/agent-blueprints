# Same-session trusted repair proof

This bounded synthetic exercise creates an intentionally incomplete `add(a, b)` candidate, downloads the saved turn's ZIP artifact, and runs the independent Phase 1B verifier against that exact SHA-256. Only an allowlisted finding code and fixed trusted message are sent back through `agent.session.input.message`. The coordinator continues in the **same Agents API session**, publishes a distinct artifact in a second turn, and the verifier tests that new artifact from scratch. [Sanitized live evidence](evidence/phase-1d-live-repair.json) records the linked session, turn, artifact, content, and verifier IDs.

The first candidate's `add` returned `0` by instruction, so its own limited tests passed while the independent requirement test failed. The repaired candidate returned `a + b`; all five trusted tests and seven candidate tests passed. This proves the mechanics of a failure and repair loop for the synthetic sample. It does not establish that the agent will recover from arbitrary defects.

## Run

From the parent `agents-api-spike` directory, use Node with the pinned SDK, a locally available pinned Phase 1B Docker image, and a private API key environment variable:

```bash
npm ci
python3 -m unittest discover -s repair-loop -p 'test_*.py' -v
node --env-file=.env.local repair-loop/run.mjs
```

The live command consumes API and hosted sandbox usage. It refuses to start another session while `.repair-runs/last-run.json` exists. That gitignored directory contains the downloaded ZIPs and a sanitized local checkpoint; inspect/reconcile an interrupted run before removing it. The script does not automatically resend an uncertain follow-up input. It subscribes to the event stream before submission and supplies a stable idempotency key, but saved turns and artifacts remain the authoritative record. [Agents API sessions](https://developers.openai.com/api/docs/guides/agents-api/sessions) describe same-session follow-up input and saved state.

No candidate-controlled test output, prompts, reasoning, API key, raw logs, or ZIP bytes are checked into evidence or sent as feedback. The finding parser recognizes known verifier test names only and maps them to fixed messages; any unrecognized or boundary failure stops the automatic repair. The trusted verifier runs candidate code in the existing pinned, network-isolated Docker configuration. The downloaded ZIP is checked by the existing strict Phase 1B intake before execution.

This original script is a live feasibility proof alongside Phase 1C; it does not write durable workflow transitions to PostgreSQL. No Jira or GitHub workflow writes are performed.

The subsequent [Phase 1D-B integrated proof](../../control-plane/PHASE-1D-RESULTS.md) connects this behavior to PostgreSQL with durable candidate and verification history, repair intent, and a real process restart. This original script remains the isolated feasibility record.
