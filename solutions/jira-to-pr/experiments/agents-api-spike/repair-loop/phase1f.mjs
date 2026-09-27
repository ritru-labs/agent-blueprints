/** Resume an existing Agents API session from a durable CI/review repair intent. */
import OpenAI from "openai";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { requireApiKey } from "../config.mjs";
import { controlCall, downloadedArtifact, admit, savedRepairMessage,
  waitForSavedRepair, waitForTurn } from "./integrated.mjs";

const hash = (raw) => createHash("sha256").update(raw).digest("hex");

async function run(task, interrupt) {
  const client = new OpenAI();
  const before = controlCall("inspect", ["--task", task]);
  if (before.state !== "VERIFIED") throw new Error("CI repair needs the current verified candidate");
  const prior = controlCall("ci-status", ["--task", task]);
  if (prior.status === "ABSENT" || prior.failed_candidate_id !== before.candidate_id) {
    const observation = controlCall("ci-observation-status", ["--task", task]);
    if (observation.status !== "OBSERVED" || observation.candidate_id !== before.candidate_id ||
        observation.gate !== "FAIL") {
      console.log(JSON.stringify({ task_key: task, state: before.state,
        candidate_count: before.candidate_count, action: "no_current_repairable_CI_failure" }));
      return;
    }
  }
  const intent = controlCall("ci-plan", ["--task", task]);
  if (intent.session_id !== before.current_session_id ||
      hash(intent.message) !== intent.input_sha256) {
    throw new Error("CI repair input does not match the durable same-session intent");
  }
  let saved = await savedRepairMessage(client, intent);
  if (intent.status === "PLANNED") {
    controlCall("ci-mark-uncertain", ["--task", task]);
    if (!saved) {
      await client.beta.agents.sessions.events.create(intent.session_id, {
        "Idempotency-Key": intent.input_key,
        events: [{ type: "agent.session.input.message", input: [{ role: "user",
          content: [{ type: "input_text", text: intent.message }] }] }],
      });
      if (interrupt) process.exit(75);
    }
  } else if (!["UNCERTAIN", "OBSERVED"].includes(intent.status)) {
    throw new Error("CI repair intent has an unsupported state");
  }
  saved = saved ?? await waitForSavedRepair(client, intent);
  controlCall("ci-observe", ["--task", task, "--session-id", intent.session_id,
    "--input-sha256", intent.input_sha256, "--message-item-id", saved.messageItemId,
    "--turn-id", saved.turnId]);
  await waitForTurn(client, intent.session_id, saved.turnId);
  const candidate = await downloadedArtifact(client, intent.session_id, saved.turnId);
  const admitted = admit(task, candidate);
  if (admitted.candidate_count !== before.candidate_count + 1 ||
      admitted.state !== "CANDIDATE_READY") {
    throw new Error("CI repair did not create one new immutable candidate");
  }
  const verified = controlCall("verify", ["--task", task]);
  if (verified.state !== "VERIFIED" || verified.verification_status !== "PASS") {
    throw new Error("fresh independent verification did not pass");
  }
  console.log(JSON.stringify({ task_key: task, state: verified.state,
    candidate_count: verified.candidate_count, verification_count: verified.verification_count,
    session_id: verified.current_session_id }));
}

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  if (!process.env.PHASE1C_DATABASE_URL || !process.env.PHASE1D_POLICY_PATH) {
    throw new Error("PostgreSQL URL and trusted run policy path are required");
  }
  const [command, flag, task] = process.argv.slice(2);
  if (!["run", "resume"].includes(command) || flag !== "--task" || !task) {
    throw new Error("usage: node phase1f.mjs run|resume --task TASK-ID");
  }
  await run(task, command === "run");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const message = String(error?.message ?? error);
    console.error(`Phase 1F repair stopped: ${process.env.OPENAI_API_KEY ?
      message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message}`);
    process.exitCode = 1;
  });
}
