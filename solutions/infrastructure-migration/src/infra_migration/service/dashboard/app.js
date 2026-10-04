(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { token: "", tenant: "", epoch: 0, roles: [], runs: [], cursor: null,
    paginated: false, selected: null, inventory: null, selectedIds: new Set(),
    intent: null, busy: false, timer: null, previewTicket: 0, controllers: new Set(), urls: new Set() };
  const names = { QUEUED: "Queued", RUNNING: "Preparing", AWAITING_REVIEW: "Awaiting review",
    REVIEW_QUEUED: "Review queued", REVIEW_RUNNING: "Recording review",
    REVIEWED_EXECUTION_BLOCKED: "Reviewed · execution blocked", REJECTED: "Rejected",
    CANCELLED: "Cancelled", FAILED: "Preparation failed" };
  const active = new Set(["QUEUED", "RUNNING", "REVIEW_QUEUED", "REVIEW_RUNNING"]);
  const available = new Set(["AWAITING_REVIEW", "REVIEWED_EXECUTION_BLOCKED", "REJECTED"]);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const supported = new Set(["AWS::EC2::VPC", "AWS::EC2::Subnet"]);
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
  }
  function color(status) {
    if (["FAILED", "REJECTED"].includes(status)) return "red";
    if (status === "AWAITING_REVIEW" || active.has(status)) return "amber";
    if (status === "REVIEWED_EXECUTION_BLOCKED") return "green";
    return "neutral";
  }
  function disconnect(message = "Disconnected. Session data has been cleared.") {
    state.epoch++;
    state.controllers.forEach((c) => c.abort());
    state.controllers.clear();
    clearTimeout(state.timer);
    state.urls.forEach((u) => URL.revokeObjectURL(u));
    state.urls.clear();
    state.token = ""; state.tenant = ""; state.roles = []; state.runs = []; state.selected = null;
    state.inventory = null; state.selectedIds.clear(); state.intent = null; state.cursor = null;
    state.busy = false; state.paginated = false; state.previewTicket++;
    $("connect-form").reset(); $("prepare-form").reset(); $("code-content").textContent = "";
    $("code-file").replaceChildren(); $("code-format").textContent = ""; $("checks").replaceChildren(); $("blockers").replaceChildren();
    $("progress").replaceChildren(); $("detail-run").textContent = "";
    $("session-org").textContent = ""; $("session-roles").textContent = "";
    delete $("decision-form").dataset.run; delete $("decision-form").dataset.digest;
    $("resource-list").replaceChildren(); $("inventory-summary").textContent = "";
    $("inventory-summary").hidden = true; $("selection").hidden = true;
    $("connection").hidden = false; $("session").hidden = true;
    $("inventory-file").disabled = true; $("prepare-button").disabled = true;
    $("refresh").disabled = true; $("connect-button").disabled = false;
    $("decision-dialog").close(); renderRuns(); renderDetails(); notice(message);
  }
  async function request(path, options = {}, format = "json") {
    const epoch = state.epoch;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    state.controllers.add(controller);
    try {
      const response = await fetch(`/v1/organizations/${encodeURIComponent(state.tenant)}${path}`, {
        method: options.method || "GET", credentials: "omit", cache: "no-store", redirect: "error",
        signal: controller.signal, headers: { Authorization: `Bearer ${state.token}`,
          ...(options.body ? { "Content-Type": "application/json" } : {}) },
        ...(options.body ? { body: JSON.stringify(options.body) } : {})
      });
      if (epoch !== state.epoch) throw new Error("SESSION_CHANGED");
      if (response.status === 401) { disconnect("Your session expired. Connect again to continue."); throw new Error("SESSION_CHANGED"); }
      if (!response.ok) {
        const messages = { 403: "This action is unavailable for your role or this package. Refresh and check the current state.",
          422: "The snapshot or request does not match the supported inventory format.",
          413: "The request is too large. Use a smaller scoped inventory.",
          408: "The request took too long. Try again with the same snapshot." };
        throw new Error(messages[response.status] || "The service could not complete this request. Try again shortly.");
      }
      const data = format === "blob" ? await response.blob() : await response.json();
      if (epoch !== state.epoch) throw new Error("SESSION_CHANGED");
      return data;
    } catch (error) {
      if (epoch !== state.epoch) throw new Error("SESSION_CHANGED");
      if (error.name === "AbortError" || error instanceof TypeError) {
        const message = options.method === "POST" ? (path === "/runs" ?
          "Request outcome unclear. Retry the same submission to recover its original run." :
          "Action outcome unclear. Refresh this preparation before submitting another action.") :
          "Service connection interrupted. Refresh to try again.";
        throw new Error(message);
      }
      throw error;
    } finally { clearTimeout(timeout); state.controllers.delete(controller); }
  }
  function report(error) { if (error.message !== "SESSION_CHANGED") notice(error.message, true); }
  function renderRuns() {
    $("count-total").textContent = state.token ? String(state.runs.length) : "—";
    $("count-review").textContent = state.token ? String(state.runs.filter((r) => r.status === "AWAITING_REVIEW").length) : "—";
    $("count-active").textContent = state.token ? String(state.runs.filter((r) => active.has(r.status)).length) : "—";
    const list = $("run-list"); list.replaceChildren();
    $("run-list-hint").textContent = state.token ? "Select a run to see checks, generated code, and its review decision." : "Connect to see your organization’s preparations.";
    if (!state.runs.length) {
      const empty = node("div", undefined, "empty-state");
      empty.append(node("span", "▤"), node("strong", state.token ? "No preparations yet" : "Your preparations will appear here"),
        node("p", "Each run keeps its checks, generated code, and review decision together.")); list.append(empty);
    }
    state.runs.forEach((run) => {
      const button = node("button", undefined, "run-item" + (state.selected?.run_id === run.run_id ? " selected" : ""));
      button.type = "button"; button.dataset.runId = run.run_id;
      button.setAttribute("aria-label", `Open preparation ${run.run_id}`);
      const label = node("span", undefined, "run-name");
      label.append(node("strong", run.run_id.slice(0, 8) + "…"), node("span", run.created_at ? new Date(run.created_at).toLocaleString() : "Preparation run"));
      const end = node("span", undefined, "run-end");
      end.append(node("span", names[run.status] || "Unknown state", "pill " + color(run.status)),
        node("span", run.result ? `${run.result.resource_count ?? run.result.resource_ids?.length ?? 0} resources` : "Checks pending"));
      button.append(label, end); button.addEventListener("click", () => selectRun(run.run_id)); list.append(button);
    });
    $("load-more").hidden = !state.cursor || state.runs.length >= 200;
  }
  async function loadRuns(more = false) {
    const data = await request("/runs" + (more && state.cursor ? `?before=${encodeURIComponent(state.cursor)}` : ""));
    const merged = new Map(state.runs.map((r) => [r.run_id, r]));
    data.runs.forEach((r) => merged.set(r.run_id, r));
    state.runs = Array.from(merged.values()).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "") || b.run_id.localeCompare(a.run_id)).slice(0, 200);
    if (more || !state.paginated) state.cursor = data.next_cursor;
    if (more) state.paginated = true;
    renderRuns();
  }
  function schedulePoll() {
    clearTimeout(state.timer);
    if (!state.token || document.hidden) return;
    state.timer = setTimeout(async () => {
      try { await loadRuns(); if (state.selected) await refreshSelected(); } catch (error) { report(error); }
      finally { schedulePoll(); }
    }, 5000);
  }
  async function refreshSelected() {
    const id = state.selected?.run_id; if (!id) return;
    const run = await request(`/runs/${encodeURIComponent(id)}`);
    if (state.selected?.run_id !== id) return;
    state.selected = run; renderDetails();
    if (available.has(run.status) && !$("code-file").options.length) await preview("index.ts");
  }
  async function selectRun(id) {
    state.previewTicket++; $("code-content").textContent = ""; $("code-file").replaceChildren();
    state.selected = { run_id: id, status: "QUEUED", result: null }; renderRuns(); renderDetails();
    try { await refreshSelected(); }
    catch (error) { report(error); }
  }
  function check(label, value) {
    const row = node("div", undefined, "check-row"); row.append(node("span", label), node("strong", value)); return row;
  }
  function renderDetails() {
    const run = state.selected;
    $("detail-content").hidden = !run; $("detail-empty").hidden = !!run;
    $("detail-status").textContent = run ? names[run.status] || "Unknown state" : "No run selected";
    $("detail-status").className = "pill " + (run ? color(run.status) : "neutral");
    if (!run) return;
    $("detail-run").textContent = `Run ${run.run_id}`;
    const stages = ["Queued", "Preparing", "Package review", "Decision recorded"];
    const index = { QUEUED: 0, RUNNING: 1, AWAITING_REVIEW: 2, REVIEW_QUEUED: 2, REVIEW_RUNNING: 2,
      REVIEWED_EXECUTION_BLOCKED: 3, REJECTED: 3, FAILED: 1, CANCELLED: 0 }[run.status] ?? 0;
    $("progress").replaceChildren(...stages.map((label, i) => node("li", label,
      i < index ? "done" : i === index ? (["FAILED", "CANCELLED", "REJECTED"].includes(run.status) ? "failed" : "current") : "")));
    const result = run.result;
    const checks = $("checks"); checks.replaceChildren();
    checks.append(check("Inventory", result ? "Unverified operator snapshot" : "Preparation pending"),
      check("Compilation", result?.compile?.passed === true ? "Passed in isolated runner" : result?.compile ? "Compiler not configured" : "Pending"),
      check("Model review", result?.model?.model ? "Completed · advisory" : result ? "Not configured" : "Pending"),
      check("Live AWS qualification", "Pending"), check("Cloud execution", "Disabled"));
    if (result?.error) checks.append(check("Preparation outcome", "Failed · operator attention needed"));
    const blockers = $("blockers"); blockers.replaceChildren();
    const explanations = { UNVERIFIED_OPERATOR_SNAPSHOT: "Uploaded inventory has not been verified against live AWS.",
      SOURCE_NOT_FROZEN: "Source automation must be frozen before ownership transfer." };
    if (!result) blockers.append(node("li", "Checks and migration blockers will appear after preparation."));
    else if (!Array.isArray(result.blockers) || !result.blockers.length) blockers.append(node("li", "Live cloud and adapter qualification are still required."));
    else result.blockers.forEach((b) => blockers.append(node("li", explanations[b] || b)));
    $("code-review").hidden = !available.has(run.status);
    $("approve").hidden = !run.can_review; $("reject").hidden = !run.can_review;
    $("cancel-run").hidden = !run.can_cancel;
    $("review-guidance").textContent = run.can_review ? "Ready for your independent package review." :
      run.status === "AWAITING_REVIEW" ? "A separate authorized reviewer must record this decision." :
      run.status === "REVIEWED_EXECUTION_BLOCKED" ? "Package reviewed. Infrastructure changes remain blocked." :
      run.status === "REJECTED" ? "Package rejected. Prepare a new package to address the findings." :
      run.status === "QUEUED" ? "Waiting for the preparation worker." :
      run.status === "FAILED" ? "Preparation stopped. Inspect the scoped inventory and service checks." :
      run.status === "CANCELLED" ? "This preparation was cancelled." : "Preparation is in progress.";
  }
  async function preview(file) {
    const run = state.selected; if (!run || !available.has(run.status)) return;
    const ticket = ++state.previewTicket;
    $("code-content").textContent = "Loading verified file…";
    try {
      const data = await request(`/runs/${encodeURIComponent(run.run_id)}/artifacts/preview?file=${encodeURIComponent(file)}`);
      if (ticket !== state.previewTicket || state.selected?.run_id !== run.run_id) return;
      if (data.artifact_digest !== state.selected.result?.artifact_digest) throw new Error("The package changed. Refresh before reviewing.");
      $("code-file").replaceChildren(...data.files.map((name) => { const option = node("option", name); option.value = name; return option; }));
      $("code-file").value = data.file;
      let content = data.content;
      if (data.file.endsWith(".json")) {
        try { content = JSON.stringify(JSON.parse(content), null, 2); } catch (_) { /* Keep exact text if not JSON. */ }
      }
      $("code-content").textContent = content;
      $("code-format").textContent = data.file.endsWith(".json") ? "JSON formatted for readability. Downloads retain the original files." : "";
    } catch (error) { if (ticket === state.previewTicket) $("code-content").textContent = "Verified preview unavailable."; report(error); }
  }
  function renderSelection() {
    const selected = state.selectedIds;
    $("selection-count").textContent = `${selected.size} selected`;
    $("prepare-button").disabled = !state.token || !selected.size || state.busy || !state.roles.includes("assessor");
    $("resource-list").replaceChildren();
    const query = $("resource-search").value.toLowerCase();
    (state.inventory?.resources || []).filter((r) => `${r.resource_id} ${r.resource_type}`.toLowerCase().includes(query)).forEach((resource) => {
      const row = node("label", undefined, "resource-row"); const box = node("input");
      box.type = "checkbox"; box.checked = selected.has(resource.resource_id); box.disabled = !supported.has(resource.resource_type);
      box.setAttribute("aria-label", `Select ${resource.resource_id}`);
      box.addEventListener("change", () => { if (box.checked) selected.add(resource.resource_id); else selected.delete(resource.resource_id); state.intent = null; renderSelection(); });
      const text = node("span", undefined, "resource-text"); text.append(node("strong", resource.resource_id), node("span", resource.resource_type));
      row.append(box, text); if (box.disabled) row.append(node("span", "Unsupported", "pill neutral")); $("resource-list").append(row);
    });
  }
  $("connect-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const tenant = $("organization").value.trim(); const token = $("access-token").value.trim();
    if (!uuid.test(tenant) || !token || token.length > 8192) return notice("Enter a valid organization ID and approved access token.", true);
    state.epoch++; state.tenant = tenant.toLowerCase(); state.token = token; $("access-token").value = ""; $("connect-button").disabled = true;
    try {
      const session = await request("/session"); state.roles = session.roles;
      $("connection").hidden = true; $("session").hidden = false;
      $("session-org").textContent = session.organization_id;
      $("session-roles").textContent = `Roles: ${session.roles.join(", ")}`;
      $("inventory-file").disabled = !state.roles.includes("assessor"); $("refresh").disabled = false;
      notice("Workspace connected. Preparation and review permissions are checked by the service.");
      await loadRuns(); schedulePoll();
    } catch (error) { if (error.message !== "SESSION_CHANGED") { disconnect(""); report(error); } }
    finally { $("connect-button").disabled = false; }
  });
  $("disconnect").addEventListener("click", () => disconnect());
  $("inventory-file").addEventListener("change", async () => {
    state.inventory = null; state.selectedIds.clear(); state.intent = null;
    $("selection").hidden = true; $("inventory-summary").hidden = true; $("prepare-button").disabled = true;
    const file = $("inventory-file").files[0]; if (!file) return;
    const epoch = state.epoch;
    try {
      if (file.size > 900000) throw new Error("Choose a scoped JSON inventory under 900 KB.");
      const inventory = JSON.parse(await file.text()); if (epoch !== state.epoch) return;
      if (inventory.scope?.tenant_id !== state.tenant || !Array.isArray(inventory.resources) || !inventory.resources.length || inventory.resources.length > 1000 ||
        inventory.resources.some((r) => typeof r.resource_id !== "string" || typeof r.resource_type !== "string")) throw new Error("Use this organization’s scoped inventory with 1–1,000 resources.");
      if (new Set(inventory.resources.map((r) => r.resource_id)).size !== inventory.resources.length) throw new Error("The snapshot contains duplicate resource IDs.");
      state.inventory = inventory; $("inventory-summary").textContent = `${file.name} · ${inventory.resources.length} resources · Account ${inventory.scope.account_id} · Unverified snapshot`;
      $("inventory-summary").hidden = false; $("selection").hidden = false; $("resource-search").value = "";
      renderSelection(); notice("Snapshot loaded. Select the supported resources you want to prepare.");
    } catch (error) { notice(error instanceof SyntaxError ? "The file is not valid JSON." : error.message, true); }
  });
  $("resource-search").addEventListener("input", renderSelection);
  $("select-supported").addEventListener("click", () => {
    state.selectedIds = new Set(state.inventory.resources.filter((r) => supported.has(r.resource_type)).map((r) => r.resource_id));
    state.intent = null; renderSelection();
  });
  $("prepare-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!state.inventory || !state.selectedIds.size || state.busy) return;
    state.busy = true; renderSelection();
    state.intent ||= crypto.randomUUID();
    try {
      const run = await request("/runs", { method: "POST", body: { inventory: state.inventory,
        resource_ids: Array.from(state.selectedIds).sort(), idempotency_key: state.intent } });
      notice("Preparation queued. The worker will assess, generate, and validate this package.");
      await loadRuns(); await selectRun(run.run_id);
    } catch (error) { report(error); }
    finally { state.busy = false; renderSelection(); }
  });
  $("refresh").addEventListener("click", async () => { try { await loadRuns(); await refreshSelected(); if (available.has(state.selected?.status) && !$("code-file").options.length) await preview("index.ts"); } catch (error) { report(error); } });
  $("load-more").addEventListener("click", async () => { try { await loadRuns(true); } catch (error) { report(error); } });
  $("code-file").addEventListener("change", () => preview($("code-file").value));
  $("download").addEventListener("click", async () => {
    const run = state.selected; if (!run) return; $("download").disabled = true;
    try {
      const blob = await request(`/runs/${encodeURIComponent(run.run_id)}/artifacts`, {}, "blob");
      const url = URL.createObjectURL(blob); state.urls.add(url);
      const link = node("a"); link.href = url; link.download = `migration-${run.run_id}.zip`; document.body.append(link); link.click(); link.remove();
      setTimeout(() => { URL.revokeObjectURL(url); state.urls.delete(url); }, 10000);
    } catch (error) { report(error); } finally { $("download").disabled = false; }
  });
  function decision(approve) {
    if (!state.selected?.can_review) return;
    $("decision-form").dataset.approve = String(approve);
    $("decision-form").dataset.run = state.selected.run_id;
    $("decision-form").dataset.digest = state.selected.result.review_digest;
    $("decision-title").textContent = approve ? "Approve this package?" : "Reject this package?";
    $("decision-copy").textContent = approve ? "Record your independent review of the exact code, checks, and blockers shown in this package." : "Record a rejection of this exact package. A new preparation is required to address the findings.";
    $("decision-submit").textContent = approve ? "Confirm approval" : "Confirm rejection";
    $("decision-check").checked = false; $("decision-dialog").showModal();
  }
  $("approve").addEventListener("click", () => decision(true)); $("reject").addEventListener("click", () => decision(false));
  $("decision-close").addEventListener("click", () => $("decision-dialog").close());
  $("decision-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = $("decision-form"); $("decision-submit").disabled = true;
    try {
      await request(`/runs/${encodeURIComponent(form.dataset.run)}/review`, { method: "POST", body: {
        plan_digest: form.dataset.digest, acknowledged: form.dataset.approve === "true" } });
      $("decision-dialog").close(); notice("Review decision queued. Infrastructure changes remain disabled.");
      await loadRuns(); await refreshSelected();
    } catch (error) { report(error); } finally { $("decision-submit").disabled = false; }
  });
  $("cancel-run").addEventListener("click", async () => {
    if (!state.selected?.can_cancel) return; $("cancel-run").disabled = true;
    try { await request(`/runs/${encodeURIComponent(state.selected.run_id)}/cancel`, { method: "POST" }); notice("Preparation cancelled."); await loadRuns(); await refreshSelected(); }
    catch (error) { report(error); } finally { $("cancel-run").disabled = false; }
  });
  document.addEventListener("visibilitychange", schedulePoll);
  window.addEventListener("pagehide", () => disconnect(""));
  $("script-required").hidden = true;
  $("connect-button").disabled = false;
})();
