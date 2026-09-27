/** Phase 1D-B live adapter. PostgreSQL owns state; this process only transports API facts. */
import OpenAI from "openai";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdir, open, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { requireApiKey } from "../config.mjs";
import { rootTurnOutcome } from "../event-log.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const spike = path.dirname(here);
const control = path.resolve(here, "../../control-plane");
const python = path.join(control, ".venv/bin/python");
const runDir = process.env.PHASE1D_RUN_DIR ?? path.join(control, ".control-runs/phase1d");
const storeDir = process.env.PHASE1D_STORE_DIR ?? path.join(control, ".control-runs/artifacts");
const artifactPath = "/workspace/outputs/sample-project.zip";
const files = ["sample/__init__.py", "sample/app.py", "sample/tests/test_app.py"];
const hash = (raw) => createHash("sha256").update(raw).digest("hex");

export function repairSubmissionDecision(state, savedMessageFound) {
  if (state === "REPAIR_INPUT_PLANNED") return savedMessageFound ? "reconcile" : "submit_once";
  if (["REPAIR_INPUT_UNKNOWN", "AWAITING_REPAIR_CANDIDATE"].includes(state)) {
    return savedMessageFound ? "reconcile" : "wait_without_resend";
  }
  throw new Error("repair submission decision requires a durable repair state");
}

export function controlCall(command, args = []) {
  const environment = {};
  for (const name of ["PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG",
                       "PHASE1C_DATABASE_URL", "TMPDIR"]) {
    if (process.env[name]) environment[name] = process.env[name];
  }
  const result = spawnSync(python, [path.join(control, "repair_cli.py"),
    "--store-dir", storeDir, "--policy", process.env.PHASE1D_POLICY_PATH ?? path.join(control, "policy.json"),
    command, ...args], {
    cwd: control, env: environment, shell: false, encoding: "utf8", timeout: 180_000,
    maxBuffer: 1_000_000,
  });
  if (result.status !== 0) {
    throw new Error(`control-plane ${command} failed: ${String(result.stderr).trim().slice(0, 500)}`);
  }
  return JSON.parse(result.stdout);
}

export async function completedInitialTurn(stream, task) {
  let sessionId = null;
  let turnId = null;
  try {
    for await (const event of stream) {
      const observed = event?.session?.id ?? event?.session_id;
      if (observed && !sessionId) {
        sessionId = observed;
        controlCall("register-session", ["--task", task, "--session-id", sessionId]);
      } else if (observed && observed !== sessionId) {
        throw new Error("initial stream switched sessions");
      }
      if (rootTurnOutcome(event) === "completed") {
        turnId = event.turn?.id ?? event.turn_id;
        break;
      }
    }
  } finally {
    stream.controller.abort();
  }
  if (!sessionId || !turnId) throw new Error("initial turn lacked saved session or turn identity");
  return { sessionId, turnId };
}

export async function downloadedArtifact(client, sessionId, turnId) {
  const turn = await client.beta.agents.sessions.turns.retrieve(turnId, { session_id: sessionId });
  if (turn.status !== "completed" || turn.subagent_id !== null || turn.session_id !== sessionId) {
    throw new Error("saved root turn is not complete in the expected session");
  }
  let artifact = null;
  for await (const item of client.beta.agents.sessions.artifacts.list(sessionId)) {
    if (item.turn_id === turnId && item.path === artifactPath) {
      if (artifact) throw new Error("saved turn has ambiguous candidate artifacts");
      artifact = item;
    }
  }
  if (!artifact || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes > 1_000_000) {
    throw new Error("saved turn lacks a bounded candidate artifact");
  }
  const response = await client.beta.agents.sessions.artifacts.content(artifact.id, { session_id: sessionId });
  const raw = Buffer.from(await response.arrayBuffer());
  if (raw.length !== artifact.size_bytes) throw new Error("downloaded artifact size mismatch");
  await mkdir(runDir, { recursive: true, mode: 0o700 });
  const digest = hash(raw);
  const localPath = path.join(runDir, `${digest}.zip`);
  try {
    const handle = await open(localPath, "wx", 0o600);
    try { await handle.writeFile(raw); } finally { await handle.close(); }
  } catch (error) {
    if (error.code !== "EEXIST" || hash(await readFile(localPath)) !== digest) throw error;
  }
  return { sessionId, turnId, artifactId: artifact.id, sha256: digest, localPath };
}

export function admit(task, candidate) {
  return controlCall("admit", ["--task", task, "--session-id", candidate.sessionId,
    "--turn-id", candidate.turnId, "--artifact-id", candidate.artifactId,
    "--archive-sha256", candidate.sha256, "--archive", candidate.localPath]);
}

export async function savedRepairMessage(client, intent) {
  const matches = [];
  for await (const item of client.beta.agents.sessions.items.list(intent.session_id)) {
    if (item.type !== "message" || item.role !== "user" || item.status !== "completed" ||
        !item.id || !item.turn_id || item.content.length !== 1 ||
        item.content[0].type !== "input_text") continue;
    if (hash(item.content[0].text) === intent.input_sha256) {
      matches.push({ messageItemId: item.id, turnId: item.turn_id });
    }
  }
  if (matches.length > 1) throw new Error("multiple saved messages match one repair intent");
  return matches[0] ?? null;
}

export async function waitForSavedRepair(client, intent) {
  for (let count = 0; count < 12; count++) {
    const saved = await savedRepairMessage(client, intent);
    if (saved) return saved;
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
  throw new Error("repair submission outcome remains unknown; no resend is permitted");
}

export async function waitForTurn(client, sessionId, turnId) {
  for (let count = 0; count < 60; count++) {
    const turn = await client.beta.agents.sessions.turns.retrieve(turnId, { session_id: sessionId });
    if (turn.status === "completed") return;
    if (["failed", "cancelled"].includes(turn.status)) throw new Error("saved repair turn did not complete");
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error("repair turn remains active; resume later without resubmitting");
}

async function start(client, task) {
  const inputs = await Promise.all(files.map(async (name) => ({
    type: "inline", path: `/workspace/${name}`,
    data: (await readFile(path.join(spike, "sample-project", name))).toString("base64"),
  })));
  const stream = await client.beta.agents.sessions.create({
    agent: { model: "gpt-6-astra", multi_agent: { enabled: false },
      instructions: "Work only on this synthetic Python project. Follow the staged repair exercise. Be honest about incomplete code and results. Do not access the network." },
    environment: { type: "openai_hosted", network: { access: "disabled" }, files: inputs },
    input: `First stage of an intentional repair exercise: add add(a, b) to ` +
      `/workspace/sample/app.py with a temporary body that returns 0 for every input. ` +
      `Keep identity(value) unchanged. Preserve the identity test and add a test for ` +
      `add(0, 0) in /workspace/sample/tests/test_app.py. Run python3 -m unittest ` +
      `discover -s sample/tests -v from /workspace. Create ${artifactPath} ` +
      `containing only sample/__init__.py, sample/app.py, sample/tests/test_app.py at ` +
      `those relative paths and read back the ZIP listing. State that the candidate ` +
      `is intentionally incomplete; do not claim the full requirement passes.`,
    stream: true,
  });
  const { sessionId, turnId } = await completedInitialTurn(stream, task);
  const first = await downloadedArtifact(client, sessionId, turnId);
  const admitted = admit(task, first);
  if (admitted.state !== "CANDIDATE_READY" || admitted.candidate_ordinal !== 1) {
    throw new Error("first candidate was not durably committed before verification");
  }
  const verified = controlCall("verify", ["--task", task]);
  if (verified.state !== "REPAIR_PENDING" || verified.verification_status !== "FAIL") {
    throw new Error("first candidate did not produce a repairable independent FAIL");
  }
  const intent = controlCall("plan", ["--task", task]);
  if (intent.session_id !== sessionId || hash(intent.message) !== intent.input_sha256 ||
      intent.ordinal !== 1 || intent.status !== "PLANNED") {
    throw new Error("durable repair input differs from same-session message");
  }
  controlCall("mark-uncertain", ["--task", task]);
  console.log(`[durable] session=${sessionId} candidate=1 verification=FAIL repair=UNCERTAIN`);
  await client.beta.agents.sessions.events.create(sessionId, {
    "Idempotency-Key": intent.input_key,
    events: [{ type: "agent.session.input.message", input: [{ role: "user",
      content: [{ type: "input_text", text: intent.message }] }] }],
  });
  // Deliberately die after the API accepts the input and before any local acknowledgement.
  process.exit(75);
}

async function resume(client, task) {
  let state = controlCall("inspect", ["--task", task]);
  if (state.state === "VERIFIED") return state;
  if (state.state === "REPAIR_PENDING") {
    controlCall("plan", ["--task", task]);
    state = controlCall("inspect", ["--task", task]);
  }
  if (state.state === "REPAIR_INPUT_PLANNED") {
    const intent = controlCall("plan", ["--task", task]);
    // Reconcile even this pre-submission state before deciding to send.
    const prior = await savedRepairMessage(client, intent);
    const decision = repairSubmissionDecision(state.state, Boolean(prior));
    controlCall("mark-uncertain", ["--task", task]);
    if (decision === "submit_once") {
      await client.beta.agents.sessions.events.create(intent.session_id, {
        "Idempotency-Key": intent.input_key,
        events: [{ type: "agent.session.input.message", input: [{ role: "user",
          content: [{ type: "input_text", text: intent.message }] }] }],
      });
    }
    state = controlCall("inspect", ["--task", task]);
  }
  if (["REPAIR_INPUT_UNKNOWN", "AWAITING_REPAIR_CANDIDATE"].includes(state.state)) {
    const intent = controlCall("plan", ["--task", task]);
    if (hash(intent.message) !== intent.input_sha256 ||
        intent.session_id !== state.current_session_id) {
      throw new Error("stored repair intent or session lineage changed");
    }
    const firstObserved = await savedRepairMessage(client, intent);
    const decision = repairSubmissionDecision(state.state, Boolean(firstObserved));
    if (decision === "submit_once") throw new Error("uncertain input cannot be resent");
    const saved = firstObserved ?? await waitForSavedRepair(client, intent);
    controlCall("observe", ["--task", task, "--session-id", intent.session_id,
      "--input-sha256", intent.input_sha256, "--message-item-id", saved.messageItemId,
      "--turn-id", saved.turnId]);
    await waitForTurn(client, intent.session_id, saved.turnId);
    const repaired = await downloadedArtifact(client, intent.session_id, saved.turnId);
    state = admit(task, repaired);
    if (state.candidate_ordinal !== intent.ordinal + 1) {
      throw new Error("repaired candidate ordinal does not follow durable repair attempt");
    }
  }
  if (!["CANDIDATE_READY", "VERIFYING"].includes(state.state)) {
    throw new Error(`cannot automatically resume from ${state.state}; no repair message was resent`);
  }
  state = controlCall("verify", ["--task", task]);
  if (state.state === "REPAIR_PENDING") return resume(client, task);
  if (state.state !== "VERIFIED" || state.verification_status !== "PASS") {
    throw new Error(`fresh repaired-candidate verification ended in ${state.state}`);
  }
  console.log(`[recovered] run=${state.run_id} session=${state.session_id} candidates=${state.candidate_count} verifications=${state.verification_count} repairs=${state.repair_count} state=${state.state}`);
  return state;
}

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  if (!process.env.PHASE1C_DATABASE_URL) throw new Error("PHASE1C_DATABASE_URL is required");
  const [command, flag, task] = process.argv.slice(2);
  if (!["start", "resume"].includes(command) || flag !== "--task" || !task) {
    throw new Error("usage: node integrated.mjs start|resume --task TASK-ID");
  }
  const client = new OpenAI();
  if (command === "start") await start(client, task);
  else await resume(client, task);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const message = String(error?.message ?? error);
    console.error(`Integrated repair stopped: ${process.env.OPENAI_API_KEY ?
      message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message}`);
    process.exitCode = 1;
  });
}
