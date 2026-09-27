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

async function main() {
  requireApiKey(process.env.OPENAI_API_KEY);
  if (!process.env.PHASE1C_DATABASE_URL || !process.env.PHASE1D_POLICY_PATH) {
    throw new Error("PostgreSQL URL and trusted run policy path are required");
  }
  const [flag, task] = process.argv.slice(2);
  if (flag !== "--task" || !task) throw new Error("usage: phase1f_qualification.mjs --task TASK-ID");
  const inputs = await Promise.all(files.map(async (name) => ({
    type: "inline", path: `/workspace/${name}`,
    data: (await readFile(path.join(spike, "sample-project", name))).toString("base64"),
  })));
  const client = new OpenAI();
  const stream = await client.beta.agents.sessions.create({
    agent: { model: "gpt-6-astra", multi_agent: { enabled: false },
      instructions: "Work only on the synthetic Python project. Follow the exact staged qualification task. Never access the network. Do not claim independent verification or CI passed." },
    environment: { type: "openai_hosted", network: { access: "disabled" }, files: inputs },
    input: "Implement add(a, b) in /workspace/sample/app.py as the arithmetic sum for positive, zero, and negative integers. Preserve identity(value). Add regression tests for add in /workspace/sample/tests/test_app.py. For this staged CI repair exercise, include the exact harmless comment # PHASE1F_REPAIR_REQUIRED in sample/app.py. Run python3 -m unittest discover -s sample/tests -v from /workspace. Create /workspace/outputs/sample-project.zip containing only sample/__init__.py, sample/app.py, and sample/tests/test_app.py at those relative paths. Read back the ZIP listing. This is Candidate #1 and the marker is intentionally left for a later trusted CI finding.",
    stream: true,
  });
  const { sessionId, turnId } = await completedInitialTurn(stream, task);
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

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const message = String(error?.message ?? error);
    console.error(`Phase 1F initial candidate stopped: ${process.env.OPENAI_API_KEY ?
      message.replaceAll(process.env.OPENAI_API_KEY, "[REDACTED]") : message}`);
    process.exitCode = 1;
  });
}
