/** Create a synthetic, independently verified candidate with a deliberate CI marker. */
import OpenAI from "openai";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { requireApiKey } from "../config.mjs";
import { completedInitialTurn, downloadedArtifact, admit, controlCall } from "./integrated.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const spike = path.dirname(here);
const files = ["sample/__init__.py", "sample/app.py", "sample/tests/test_app.py"];

async function commitInitialCandidate(client, task, sessionId, turnId) {
  const candidate = await downloadedArtifact(client, sessionId, turnId);
  const admitted = admit(task, candidate);
  if (admitted.state !== "CANDIDATE_READY" || admitted.candidate_ordinal !== 1) {
    throw new Error("initial candidate was not committed once");
  }
  const verified = controlCall("verify", ["--task", task]);
  if (verified.state !== "VERIFIED" || verified.verification_status !== "PASS") {
    throw new Error("initial candidate did not pass fresh independent verification");
  }
  console.log(JSON.stringify({ task_key: task, session_id: sessionId,
    candidate_id: verified.candidate_id, verification_id: verified.verification_id,
    artifact_sha256: verified.artifact_sha256, state: verified.state }));
}

export async function savedInitialTurn(client, sessionId) {
  const roots = [];
  let count = 0;
  for await (const turn of client.beta.agents.sessions.turns.list(sessionId)) {
    if (++count > 100 || turn.session_id !== sessionId) {
      throw new Error("saved initial session has ambiguous turn lineage");
    }
    if (turn.subagent_id === null) roots.push(turn.id);
  }
  if (roots.length !== 1) throw new Error("saved initial session needs exactly one root turn");
  for (let attempt = 0; attempt < 60; attempt++) {
    const turn = await client.beta.agents.sessions.turns.retrieve(roots[0], { session_id: sessionId });
    if (turn.session_id !== sessionId || turn.subagent_id !== null) {
      throw new Error("saved initial turn changed identity");
    }
    if (turn.status === "completed") return turn.id;
    if (["failed", "cancelled"].includes(turn.status)) {
      const code = typeof turn.error?.code === "string" &&
        /^[a-z0-9_]{1,80}$/.test(turn.error.code) ? turn.error.code : "unknown";
      throw new Error(`saved initial turn ended ${turn.status} (${code}); no input was resent`);
    }
    if (!["queued", "in_progress", "waiting"].includes(turn.status)) {
      throw new Error("saved initial turn has unknown status");
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error("saved initial turn remains active; resume later without creating a session");
}

async function start(client, task) {
  const inputs = await Promise.all(files.map(async (name) => ({
    type: "inline", path: `/workspace/${name}`,
    data: (await readFile(path.join(spike, "sample-project", name))).toString("base64"),
  })));
  const stream = await client.beta.agents.sessions.create({
    agent: { model: "gpt-6-astra", multi_agent: { enabled: false },
      instructions: "Work only on the synthetic Python project. Follow the exact staged qualification task. Never access the network. Do not claim independent verification or CI passed." },
    environment: { type: "openai_hosted", network: { access: "disabled" }, files: inputs },
    input: "Implement add(a, b) in /workspace/sample/app.py as the arithmetic sum for positive, zero, and negative integers. Preserve identity(value). Add regression tests for add in /workspace/sample/tests/test_app.py. For this staged CI repair exercise, include the exact harmless comment # PHASE1F_REPAIR_REQUIRED in sample/app.py. Run python3 -m unittest discover -s sample/tests -v from /workspace. Create /workspace/outputs/sample-project.zip containing only sample/__init__.py, sample/app.py, and sample/tests/test_app.py at those relative paths. Read back the ZIP listing. This is Candidate #1 and the marker is intentionally left for a later trusted CI finding.",
    stream: true,
  });
  const { sessionId, turnId } = await completedInitialTurn(stream, task);
  await commitInitialCandidate(client, task, sessionId, turnId);
}

async function resume(client, task) {
  const state = controlCall("inspect", ["--task", task]);
  if (state.state === "VERIFIED") return;
  if (state.state === "RECEIVED") {
    const turnId = await savedInitialTurn(client, state.current_session_id);
    await commitInitialCandidate(client, task, state.current_session_id, turnId);
    return;
  }
  if (["CANDIDATE_READY", "VERIFYING"].includes(state.state)) {
    const verified = controlCall("verify", ["--task", task]);
    if (verified.state !== "VERIFIED" || verified.verification_status !== "PASS") {
      throw new Error("saved initial candidate did not pass fresh independent verification");
    }
    return;
  }
  throw new Error(`initial recovery cannot continue from ${state.state}`);
}

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  if (!process.env.PHASE1C_DATABASE_URL || !process.env.PHASE1D_POLICY_PATH) {
    throw new Error("PostgreSQL URL and trusted run policy path are required");
  }
  const [command, flag, task] = process.argv.slice(2);
  if (!["start", "resume"].includes(command) || flag !== "--task" || !task) {
    throw new Error("usage: phase1f_qualification.mjs start|resume --task TASK-ID");
  }
  const client = new OpenAI();
  if (command === "start") await start(client, task);
  else await resume(client, task);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const message = String(error?.message ?? error);
    console.error(`Phase 1F initial candidate stopped: ${process.env.OPENAI_API_KEY ?
      message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message}`);
    process.exitCode = 1;
  });
}
