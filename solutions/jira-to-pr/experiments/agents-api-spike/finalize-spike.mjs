import { readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";

export const artifactPath = "/workspace/outputs/sample-project.zip";

// Reconcile durable session records. Streams can omit intermediate subagent events.
export async function finalizeSpike(client, run, here) {
  if (!run.sessionId || !run.turnId || !run.subagentIds?.length) {
    throw new Error("Session, root turn, or observed subagent ID is missing.");
  }
  const root = await client.beta.agents.sessions.turns.retrieve(run.turnId, { session_id: run.sessionId });
  if (root.status !== "completed" || root.subagent_id !== null) {
    throw new Error(`Root turn is not completed: ${root.status}.`);
  }
  run.subagentTurns = [];
  for (const id of run.subagentIds) {
    for await (const turn of client.beta.agents.sessions.subagents.turns.list(id, { session_id: run.sessionId })) {
      if (turn.status !== "completed" || turn.subagent_id !== id) continue;
      for await (const item of client.beta.agents.sessions.subagents.turns.items.list(turn.id, {
        session_id: run.sessionId, subagent_id: id,
      })) {
        if (item.type === "message" && item.role === "assistant" && item.status === "completed") {
          run.subagentTurns.push({ subagentId: id, turnId: turn.id, messageItemId: item.id });
          break;
        }
      }
    }
  }
  if (run.subagentTurns.length < 1) {
    throw new Error("No completed native subagent turn with an assistant message was found in saved records.");
  }

  let testItem = null;
  for await (const item of client.beta.agents.sessions.items.list(run.sessionId)) {
    if (item.turn_id === run.turnId && item.type === "command_execution" &&
        String(item.command).includes("unittest") && item.exit_code === 0) {
      testItem = item;
      break;
    }
  }
  if (!testItem) throw new Error("No passing unittest command item was found in the saved root turn.");
  run.test = {
    itemId: testItem.id,
    exitCode: testItem.exit_code,
    outputExcerpt: String(testItem.output ?? "").slice(0, 500),
  };

  let artifact = null;
  for await (const candidate of client.beta.agents.sessions.artifacts.list(run.sessionId)) {
    if (candidate.turn_id === run.turnId && candidate.path === artifactPath) {
      artifact = candidate;
      break;
    }
  }
  if (!artifact) throw new Error(`No published artifact at ${artifactPath} for completed turn ${run.turnId}.`);
  if (artifact.size_bytes > 1_000_000) throw new Error("Published archive exceeds spike size limit.");
  const response = await client.beta.agents.sessions.artifacts.content(artifact.id, { session_id: run.sessionId });
  const destination = path.join(here, ".spike-runs", "sample-project.zip");
  await writeFile(destination, Buffer.from(await response.arrayBuffer()));
  const validation = spawnSync("python3", [
    path.join(here, "validate_artifact.py"), destination,
    path.join(here, "sample-project/sample/app.py"),
    path.join(here, "sample-project/sample/tests/test_app.py"),
  ], { encoding: "utf8", shell: false });
  if (validation.status !== 0) throw new Error(validation.stderr.trim() || "Downloaded artifact failed validation.");
  run.artifact = { id: artifact.id, path: artifact.path, turnId: artifact.turn_id, ...JSON.parse(validation.stdout) };
  run.status = "passed";
  delete run.error;
  return run;
}

export async function saveRun(here, run) {
  await writeFile(path.join(here, ".spike-runs", "last-run.json"), JSON.stringify(run, null, 2) + "\n");
}

export async function loadRun(here) {
  return JSON.parse(await readFile(path.join(here, ".spike-runs", "last-run.json"), "utf8"));
}
