import OpenAI from "openai";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdir, open, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { requireApiKey } from "../config.mjs";
import { rootTurnOutcome } from "../event-log.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const spike = path.dirname(here);
const runDir = path.join(spike, ".repair-runs");
const runFile = path.join(runDir, "last-run.json");
const artifactPath = "/workspace/outputs/sample-project.zip";
const files = ["sample/__init__.py", "sample/app.py", "sample/tests/test_app.py"];

async function checkpoint(record) {
  await writeFile(runFile, `${JSON.stringify(record, null, 2)}\n`, { mode: 0o600 });
}

async function completedTurn(stream, sessionId = null, record = null) {
  let turnId = null;
  try {
    for await (const event of stream) {
      const observedSession = event?.session?.id ?? event?.session_id;
      if (observedSession) {
        if (sessionId && observedSession !== sessionId) throw new Error("stream switched sessions");
        sessionId = observedSession;
        if (record && !record.sessionId) {
          record.sessionId = sessionId;
          await checkpoint(record);
        }
      }
      if (rootTurnOutcome(event) === "completed") {
        turnId = event.turn?.id ?? event.turn_id;
        break;
      }
    }
  } finally {
    stream.controller.abort();
  }
  if (!sessionId || !turnId) throw new Error("completed root turn was not observed; reconcile saved state");
  return { sessionId, turnId };
}

async function downloadCandidate(client, sessionId, turnId, stage) {
  const turn = await client.beta.agents.sessions.turns.retrieve(turnId, { session_id: sessionId });
  if (turn.status !== "completed" || turn.subagent_id !== null) {
    throw new Error("saved root turn is not completed");
  }
  let artifact = null;
  for await (const item of client.beta.agents.sessions.artifacts.list(sessionId)) {
    if (item.turn_id === turnId && item.path === artifactPath) {
      artifact = item;
      break;
    }
  }
  if (!artifact || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes > 1_000_000) {
    throw new Error("saved turn has no bounded candidate artifact");
  }
  const response = await client.beta.agents.sessions.artifacts.content(artifact.id, { session_id: sessionId });
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length !== artifact.size_bytes) throw new Error("downloaded artifact size differs from saved metadata");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  const destination = path.join(runDir, `${stage}-${sha256}.zip`);
  const handle = await open(destination, "wx", 0o600);
  try {
    await handle.writeFile(bytes);
  } finally {
    await handle.close();
  }
  return { sessionId, turnId, artifactId: artifact.id, path: artifact.path,
    sizeBytes: bytes.length, sha256, localPath: destination };
}

function trustedVerify(candidate) {
  const result = spawnSync("python3", [path.join(here, "trusted_verify.py"),
    "--artifact", candidate.localPath, "--expected-sha256", candidate.sha256],
  { encoding: "utf8", shell: false, timeout: 180_000, maxBuffer: 100_000 });
  if (result.status !== 0) throw new Error("independent verifier could not establish a result");
  const proof = JSON.parse(result.stdout);
  if (proof.artifact_sha256 !== candidate.sha256 || !["PASS", "FAIL"].includes(proof.status)) {
    throw new Error("trusted result does not match the downloaded candidate");
  }
  return proof;
}

function repairMessage(failed) {
  if (failed.verification.status !== "FAIL" ||
      failed.verification.findings.length !== 1 ||
      failed.verification.findings[0].code !== "ADD_ARITHMETIC") {
    throw new Error("verification failure has no approved sanitized repair finding");
  }
  const finding = failed.verification.findings[0];
  return `The independent trusted verifier rejected artifact SHA-256 ${failed.sha256}. ` +
    `Finding ${finding.code}: ${finding.message} ` +
    `Repair /workspace/sample/app.py, add regression tests in /workspace/sample/tests/test_app.py, ` +
    `run python3 -m unittest discover -s sample/tests -v from /workspace, then create a fresh ` +
    `${artifactPath} ZIP with only sample/__init__.py, sample/app.py, and ` +
    `sample/tests/test_app.py. Read back the ZIP listing. This is a new candidate; ` +
    `do not claim trusted verification passed.`;
}

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  await mkdir(runDir, { recursive: true, mode: 0o700 });
  try {
    await readFile(runFile);
    throw new Error("a previous repair run exists; reconcile it before a new live session");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const client = new OpenAI();
  const inputs = await Promise.all(files.map(async (name) => ({
    type: "inline", path: `/workspace/${name}`,
    data: (await readFile(path.join(spike, "sample-project", name))).toString("base64"),
  })));
  const record = { schemaVersion: 1, status: "starting", sessionId: null,
    first: null, repairInputKey: null, repaired: null };
  await checkpoint(record);

  const initial = await client.beta.agents.sessions.create({
    agent: { model: "gpt-6-astra", multi_agent: { enabled: false },
      instructions: "Work only on this synthetic Python project. Follow the staged repair exercise exactly. Be honest about incomplete code, test results, and artifact creation. Never access the network." },
    environment: { type: "openai_hosted", network: { access: "disabled" }, files: inputs },
    input: `This is a deliberate two-stage repair exercise. In this first stage only, add a temporary ` +
      `implementation of add(a, b) in /workspace/sample/app.py that returns 0 for every input. ` +
      `Keep identity(value) unchanged. Add a candidate test for add(0, 0) in ` +
      `/workspace/sample/tests/test_app.py while preserving its identity test. Run ` +
      `python3 -m unittest discover -s sample/tests -v from /workspace. ` +
      `Create ${artifactPath} with only sample/__init__.py, sample/app.py, and ` +
      `sample/tests/test_app.py at those exact relative paths; read back its ZIP listing. ` +
      `Label this candidate intentionally incomplete and do not claim the full addition requirement passes.`,
    stream: true,
  });
  const firstTurn = await completedTurn(initial, null, record);
  record.status = "first_turn_completed";
  await checkpoint(record);
  record.first = await downloadCandidate(client, firstTurn.sessionId, firstTurn.turnId, "first");
  record.first.verification = trustedVerify(record.first);
  record.status = "first_verified";
  await checkpoint(record);
  if (record.first.verification.status !== "FAIL") {
    throw new Error("deliberately incomplete first candidate did not fail trusted verification");
  }
  console.log(`[first] turn=${record.first.turnId} artifact=${record.first.artifactId} trusted=FAIL`);

  const feedback = repairMessage(record.first);
  const stream = await client.beta.agents.sessions.events.stream(record.sessionId);
  record.repairInputKey = createHash("sha256").update(`${record.sessionId}:${record.first.artifactId}:repair`).digest("hex");
  record.status = "repair_input_planned";
  await checkpoint(record);
  try {
    await client.beta.agents.sessions.events.create(record.sessionId, {
      "Idempotency-Key": record.repairInputKey,
      events: [{ type: "agent.session.input.message", input: [{ role: "user",
        content: [{ type: "input_text", text: feedback }] }] }],
    });
    record.status = "repair_input_accepted";
    await checkpoint(record);
    const secondTurn = await completedTurn(stream, record.sessionId);
    record.repaired = await downloadCandidate(client, secondTurn.sessionId, secondTurn.turnId, "repaired");
  } finally {
    stream.controller.abort();
  }
  if (record.repaired.turnId === record.first.turnId ||
      record.repaired.artifactId === record.first.artifactId ||
      record.repaired.sha256 === record.first.sha256) {
    throw new Error("repair did not publish a distinct candidate and turn");
  }
  record.repaired.verification = trustedVerify(record.repaired);
  record.status = record.repaired.verification.status === "PASS" ? "passed" : "repair_failed";
  await checkpoint(record);
  console.log(`[repaired] session=${record.sessionId} turn=${record.repaired.turnId} artifact=${record.repaired.artifactId} trusted=${record.repaired.verification.status}`);
  if (record.status !== "passed") throw new Error("fresh trusted verification did not pass");
}

main().catch(async (error) => {
  const message = String(error?.message ?? error);
  const safe = process.env.OPENAI_API_KEY ?
    message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message;
  console.error(`Repair loop stopped: ${safe}`);
  process.exitCode = 1;
});
