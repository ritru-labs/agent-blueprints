import assert from "node:assert/strict";
import test from "node:test";
import { savedInitialSession, savedInitialTurn } from "../repair-loop/phase1f_qualification.mjs";

function savedClient(turns, result) {
  return { beta: { agents: { sessions: { turns: {
    async *list(sessionId) {
      assert.equal(sessionId, "sess-1");
      yield* turns;
    },
    async retrieve(turnId, params) {
      assert.equal(turnId, "turn-1");
      assert.deepEqual(params, { session_id: "sess-1" });
      return result;
    },
  } } } } };
}

test("initial recovery uses one completed saved root turn", async () => {
  const root = { id: "turn-1", session_id: "sess-1", subagent_id: null };
  assert.equal(await savedInitialTurn(savedClient([root], {
    ...root, status: "completed", error: null,
  }), "sess-1"), "turn-1");
});

test("failed initial turn cannot create or resend work", async () => {
  const root = { id: "turn-1", session_id: "sess-1", subagent_id: null };
  await assert.rejects(savedInitialTurn(savedClient([root], {
    ...root, status: "failed", error: { code: "usage_limit_exceeded", message: "untrusted detail" },
  }), "sess-1"), (error) => {
    assert.match(error.message, /usage_limit_exceeded/);
    assert.doesNotMatch(error.message, /untrusted detail/);
    return true;
  });
});

test("ambiguous saved initial turns fail closed", async () => {
  const root = { id: "turn-1", session_id: "sess-1", subagent_id: null };
  await assert.rejects(savedInitialTurn(savedClient([root, { ...root, id: "turn-2" }], null),
    "sess-1"), /exactly one root turn/);
});

function sessionClient(sessions) {
  return { beta: { agents: { sessions: {
    async *list() { yield* sessions; },
  } } } };
}

test("uncertain create reconciles only exact saved task metadata", async () => {
  const matching = { id: "sess-1", metadata: {
    ritru_task_key: "GH-CI-1", ritru_initial_key: "key-1",
  } };
  assert.equal(await savedInitialSession(sessionClient([matching]), "GH-CI-1", "key-1"),
    "sess-1");
  assert.equal(await savedInitialSession(sessionClient([matching]), "GH-CI-2", "key-1"),
    null);
  await assert.rejects(savedInitialSession(sessionClient([matching,
    { ...matching, id: "sess-2" }]), "GH-CI-1", "key-1"),
  /conflicting saved sessions/);
});
