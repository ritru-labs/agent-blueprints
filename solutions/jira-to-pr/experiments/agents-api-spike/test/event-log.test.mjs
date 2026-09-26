import assert from "node:assert/strict";
import test from "node:test";
import { createEventLog, rootTurnOutcome } from "../event-log.mjs";

test("records observed subagent and command evidence without message text", () => {
  const lines = [];
  const log = createEventLog((line) => lines.push(line));
  log.record({ type: "agent.session.created", event_id: "evt_1", session: { id: "sess_1" } });
  log.record({ type: "agent.session.subagent.created", event_id: "evt_2", subagent: { id: "sub_1" } });
  log.record({ type: "agent.session.turn.item.done", event_id: "evt_3", session_id: "sess_1", item: { id: "item_1", type: "command_execution", turn_id: "turn_1", command: "python3 -m unittest", exit_code: 0 } });
  log.record({ type: "agent.session.turn.completed", event_id: "evt_4", session_id: "sess_1", turn: { id: "turn_1", subagent_id: null } });
  assert.equal(log.state.sessionId, "sess_1");
  assert.equal(log.state.rootTurnId, "turn_1");
  assert.deepEqual([...log.state.subagentIds], ["sub_1"]);
  assert.equal(log.state.commands[0].exitCode, 0);
  assert.equal(lines.length, 4);
  assert.equal(JSON.stringify(log.state).includes("reasoning"), false);
});

test("subagent completion is not root success and root failure is fatal", () => {
  assert.equal(rootTurnOutcome({ type: "agent.session.turn.completed", turn: { id: "subturn", subagent_id: "sub_1" } }), null);
  assert.equal(rootTurnOutcome({ type: "agent.session.turn.completed", turn: { id: "rootturn", subagent_id: null } }), "completed");
  assert.throws(() => rootTurnOutcome({ type: "agent.session.turn.failed", turn: { subagent_id: null } }), /Root turn stopped/);
});
