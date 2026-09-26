// Summarize only observed API events. Do not record message content or reasoning.
export function createEventLog(writeLine = console.log) {
  const state = {
    sessionId: null,
    rootTurnId: null,
    subagentIds: new Set(),
    completedSubagentIds: new Set(),
    commands: [],
    events: [],
  };

  function record(event) {
    const type = event?.type;
    const id = event?.event_id ?? null;
    const sessionId = event?.session?.id ?? event?.session_id ?? null;
    const turnId = event?.turn?.id ?? event?.turn_id ?? null;
    const subagentId = event?.subagent?.id ?? event?.turn?.subagent_id ?? null;
    const item = event?.item;
    if (sessionId) state.sessionId = sessionId;

    let line = null;
    if (type === "agent.session.created") {
      line = `[session] created id=${sessionId} event=${id}`;
    } else if (type === "agent.session.environment.connected") {
      line = `[environment] connected id=${event.environment?.id ?? "unknown"} event=${id}`;
    } else if (type === "agent.session.turn.created") {
      line = `[turn] created id=${turnId} subagent=${subagentId ?? "root"} event=${id}`;
    } else if (type === "agent.session.subagent.created") {
      if (subagentId) state.subagentIds.add(subagentId);
      line = `[subagent] created id=${subagentId ?? "unknown"} event=${id}`;
    } else if (type === "agent.session.subagent.closed") {
      line = `[subagent] closed id=${subagentId ?? "unknown"} event=${id}`;
    } else if (type === "agent.session.turn.item.done" && item?.type === "command_execution") {
      const command = String(item.command ?? "").slice(0, 180).replaceAll("\n", " ");
      state.commands.push({ id: item.id, turnId: item.turn_id, command, exitCode: item.exit_code });
      line = `[command] item=${item.id} turn=${item.turn_id} exit=${item.exit_code} command=${command}`;
    } else if (type === "agent.session.turn.item.done" && ["create_subagent_call", "wait_for_subagents_call"].includes(item?.type)) {
      line = `[delegation] item=${item.id} type=${item.type} status=${item.status} event=${id}`;
    } else if (type === "agent.session.turn.completed") {
      if (subagentId) state.completedSubagentIds.add(subagentId);
      else state.rootTurnId = turnId;
      line = `[turn] completed id=${turnId} subagent=${subagentId ?? "root"} event=${id}`;
    } else if (["agent.session.turn.failed", "agent.session.turn.cancelled", "agent.session.failed", "agent.session.environment.failed", "error", "agent.session.requires_action"].includes(type)) {
      line = `[lifecycle] ${type} turn=${turnId ?? "none"} event=${id}`;
    }

    if (line) {
      state.events.push({ type, eventId: id, sessionId, turnId, subagentId, itemId: item?.id ?? null });
      writeLine(line);
    }
  }

  return { state, record };
}

export function rootTurnOutcome(event) {
  if (event?.type === "error") throw new Error(event.error?.message ?? "Agents API stream error");
  if (["agent.session.failed", "agent.session.environment.failed", "agent.session.requires_action"].includes(event?.type)) {
    throw new Error(`Agents API lifecycle stopped: ${event.type}`);
  }
  if (event?.turn?.subagent_id != null) return null;
  if (event?.type === "agent.session.turn.completed") return "completed";
  if (["agent.session.turn.failed", "agent.session.turn.cancelled"].includes(event?.type)) {
    throw new Error(`Root turn stopped: ${event.type}: ${event.turn?.error?.message ?? ""}`);
  }
  return null;
}
