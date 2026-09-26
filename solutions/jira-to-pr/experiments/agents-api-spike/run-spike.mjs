import OpenAI from "openai";
import { readFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createEventLog, rootTurnOutcome } from "./event-log.mjs";
import { requireApiKey } from "./config.mjs";
import { artifactPath, finalizeSpike, saveRun } from "./finalize-spike.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const runDir = path.join(here, ".spike-runs");
const inputFiles = ["sample/__init__.py", "sample/app.py", "sample/tests/test_app.py"];

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  const client = new OpenAI();
  if (typeof client.beta?.agents?.sessions?.create !== "function" ||
      typeof client.beta.agents.sessions.artifacts?.list !== "function" ||
      typeof client.beta.agents.sessions.artifacts?.content !== "function") {
    throw new Error("Installed OpenAI SDK lacks the documented beta Agents session/artifact methods.");
  }
  const files = await Promise.all(inputFiles.map(async (name) => ({
    type: "inline",
    path: `/workspace/${name}`,
    data: (await readFile(path.join(here, "sample-project", name))).toString("base64"),
  })));
  const log = createEventLog();
  const run = { status: "started", sessionId: null, turnId: null, subagentIds: [], events: [], commands: [] };
  await mkdir(runDir, { recursive: true });
  try {
    const stream = await client.beta.agents.sessions.create({
      agent: {
        model: "gpt-6-astra",
        instructions: "Work only on this synthetic Python project. First create exactly one subagent to inspect the project and recommend tests; tell it not to modify files. Wait for its response. As coordinator, make all file edits yourself. Run the actual tests and report the result. Never claim a command or artifact succeeded without checking it.",
        multi_agent: { enabled: true, max_concurrent_subagents: 1 },
      },
      environment: { type: "openai_hosted", network: { access: "disabled" }, files },
      input: `Inspect /workspace/sample. Ask one subagent for read-only test recommendations for add(a, b). After it replies, you must modify /workspace/sample/app.py to add add(a, b), modify /workspace/sample/tests/test_app.py with a real add test, and run "python3 -m unittest discover -s sample/tests -v" from /workspace. If tests pass, use Python's zipfile to create ${artifactPath} containing only sample/__init__.py, sample/app.py, and sample/tests/test_app.py with those exact relative names. Read back the ZIP listing. If a step fails, report the failure honestly.`,
      stream: true,
    });
    let completed = false;
    try {
      for await (const event of stream) {
        log.record(event);
        if (rootTurnOutcome(event) === "completed") {
          completed = true;
          break;
        }
      }
    } finally {
      stream.controller.abort();
    }
    if (!completed) throw new Error("Event stream ended without a completed root turn; reconcile session state before retrying.");
    const state = log.state;
    run.sessionId = state.sessionId;
    run.turnId = state.rootTurnId;
    run.subagentIds = [...state.subagentIds];
    run.events = state.events;
    run.commands = state.commands;
    if (!run.sessionId || !run.turnId) throw new Error("Completed turn lacked a session or turn ID.");
    await finalizeSpike(client, run, here);
    console.log(`[subagent] saved turn=${run.subagentTurns[0].turnId} message=${run.subagentTurns[0].messageItemId}`);
    console.log(`[artifact] id=${run.artifact.id} path=${run.artifact.path} sha256=${run.artifact.sha256}`);
  } catch (error) {
    run.status = "failed";
    run.error = String(error?.message ?? error).replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]");
    run.sessionId ??= log.state.sessionId;
    run.turnId ??= log.state.rootTurnId;
    run.subagentIds = [...log.state.subagentIds];
    run.events = log.state.events;
    run.commands = log.state.commands;
    throw error;
  } finally {
    await saveRun(here, run);
    console.log(`[spike] ${run.status}; sanitized evidence: ${path.join(runDir, "last-run.json")}`);
  }
}

main().catch((error) => {
  const message = String(error?.message ?? error);
  console.error(`Spike failed: ${process.env.OPENAI_API_KEY ? message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message}`);
  process.exitCode = 1;
});
