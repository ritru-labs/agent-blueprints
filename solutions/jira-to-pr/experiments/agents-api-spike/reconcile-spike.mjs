// Read-only API reconciliation for a previously completed live turn. No new turn.
import OpenAI from "openai";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { requireApiKey } from "./config.mjs";
import { finalizeSpike, loadRun, saveRun } from "./finalize-spike.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
requireApiKey(process.env.OPENAI_API_KEY);
const run = await loadRun(here);
try {
  await finalizeSpike(new OpenAI(), run, here);
  console.log(`[subagent] saved turn=${run.subagentTurns[0].turnId} message=${run.subagentTurns[0].messageItemId}`);
  console.log(`[artifact] id=${run.artifact.id} sha256=${run.artifact.sha256}`);
} catch (error) {
  run.status = "failed";
  run.error = String(error?.message ?? error).replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]");
  console.error(`Reconciliation failed: ${run.error}`);
  process.exitCode = 1;
} finally {
  await saveRun(here, run);
}
