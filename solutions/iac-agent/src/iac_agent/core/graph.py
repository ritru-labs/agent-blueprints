"""The adoption pipeline (CLAUDE.md steps 1-10) as a LangGraph graph.

guard -> discover -> classify -> scope_signoff* -> generate -> static <-> repair
      -> plan <-> repair -> approve* -> rescan -> import -> verify -> report
(* = human interrupt). Every exit, good or bad, goes through `report`.

Cloud-neutral: everything cloud-specific comes from the Adapter. Side effects
come from injected Deps, so the graph runs against fakes in tests. The
checkpointer (SQLite) makes a run resumable across processes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import hcl, naming
from .gates import config_gate, coverage_gate, fingerprint_gate, plan_gate, static_gate, verify_gate
from .models import Classification, Discovery, Finding, GateOutcome, GateResult, ScopeItem
from .repair import MAX_ATTEMPTS, Repairer, repair_block
from .report import adoption_report, findings_report
from .terraform import Terraform, TerraformError, sha256_file, tool_versions

GENERATED, IMPORTS, PLANFILE = "generated.tf", "imports.tf", "tfplan"
FINAL = {"blocked", "hard_stop", "failed", "rejected", "adopted", "nothing_to_adopt"}


class Adapter(Protocol):
    inline_blocks: Mapping[str, set[str]]
    identity_attrs: Mapping[str, set[str]]
    secret_attrs: Mapping[str, set[str]]  # blocks holding these are never sent to the LLM

    def identity(self) -> tuple[str, str]: ...
    def discover(self) -> Discovery: ...
    def classify(self, discovery: Discovery) -> list[Classification]: ...
    def provider_files(self, versions: Mapping[str, str], account: str) -> dict[str, str]: ...


class ScannersLike(Protocol):
    def tflint(self, workdir: Path) -> list[dict]: ...
    def gitleaks(self, workdir: Path) -> list[dict]: ...
    def checkov(self, workdir: Path) -> list[dict]: ...


@dataclass
class Deps:
    adapter: Adapter
    terraform: Terraform
    scanners: ScannersLike
    repairer: Repairer
    workdir: Path
    max_restarts: int = 3
    backend_hcl: str | None = None  # backend.tf content; local state when None


class RunState(TypedDict, total=False):
    run_id: str
    account: str  # signed-off account and region
    region: str
    status: str
    discovery: dict
    classifications: list[dict]
    candidates: list[dict]
    approved: list[list[str]]
    scope: list[dict]
    fingerprints: dict[str, str]
    attempts: dict[str, int]
    skipped: dict[str, str]
    pending: list[dict]  # findings waiting for the repair node
    gates: list[dict]
    plan_gate: dict
    restarts: int
    adopted: list[str]


def _gate(state: RunState, result: GateResult) -> list[dict]:
    return [*state.get("gates", []), result.model_dump(mode="json")]


def _active(state: RunState) -> list[ScopeItem]:
    skipped = state.get("skipped", {})
    return [ScopeItem(**s) for s in state.get("scope", []) if s["address"] not in skipped]


def build(deps: Deps, checkpointer: Any = None):
    wd, tf, adapter = deps.workdir, deps.terraform, deps.adapter

    def read(name: str) -> str:
        path = wd / name
        return path.read_text() if path.exists() else ""

    def write(name: str, text: str) -> None:
        (wd / name).write_text(text)

    # --- 1. Account guard ------------------------------------------------------
    def guard(state: RunState) -> RunState:
        account, region = adapter.identity()
        findings = []
        if (account, region) != (state["account"], state["region"]):
            findings.append(Finding(outcome=GateOutcome.BLOCKED,
                                    message=f"credentials are for {account}/{region}, "
                                            f"signed off {state['account']}/{state['region']}"))  # fmt: skip
        result = GateResult(gate="guard", findings=findings)
        return {"gates": _gate(state, result), "status": "running" if result.passed else "blocked"}

    # --- 2. Discover -------------------------------------------------------------
    def discover(state: RunState) -> RunState:
        found = adapter.discover()
        result = coverage_gate(found.incomplete)
        return {
            "discovery": found.model_dump(mode="json"),
            "gates": _gate(state, result),
            "status": "running" if result.passed else "blocked",
        }

    # --- 3. Classify -----------------------------------------------------------------
    def classify(state: RunState) -> RunState:
        found = Discovery(**state["discovery"])
        classes = adapter.classify(found)
        adoptable = [c.resource for c in classes if c.adoptable]
        addresses = naming.addresses(adoptable)
        candidates = [
            ScopeItem(terraform_type=r.terraform_type, import_id=r.import_id, address=addresses[r.key])
            for r in sorted(adoptable, key=lambda r: addresses[r.key])
        ]
        return {
            "classifications": [c.model_dump(mode="json") for c in classes],
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }

    # --- 4. Scope sign-off (human) -------------------------------------------------
    def scope_signoff(state: RunState) -> RunState:
        candidates = [ScopeItem(**c) for c in state["candidates"]]
        keys = {(c.terraform_type, c.import_id) for c in candidates}
        approved = {tuple(k) for k in state.get("approved") or []}
        if not approved or not approved <= keys:  # first time, or the scope changed on restart
            answer = interrupt({"step": "scope_signoff", "candidates": state["candidates"]})
            approved = keys if answer == "all" else {tuple(k) for k in answer}
        ignored = sorted(approved - keys)
        scope = [c for c in candidates if (c.terraform_type, c.import_id) in approved]
        result = GateResult(gate="scope", findings=[
            Finding(outcome=GateOutcome.PASS, message=f"not adoptable, ignored: {t} {i}") for t, i in ignored
        ])  # fmt: skip
        fingerprints = Discovery(**state["discovery"]).fingerprints({(s.terraform_type, s.import_id) for s in scope})
        return {
            "approved": [list(k) for k in sorted(approved & keys)],
            "scope": [s.model_dump(mode="json") for s in scope],
            "fingerprints": fingerprints,
            "attempts": {},
            "skipped": {},
            "gates": _gate(state, result),
            "status": "running" if scope else "nothing_to_adopt",
        }

    # --- 5. Generate: Terraform's own config generation is the first draft ---------
    def generate(state: RunState) -> RunState:
        for name in (GENERATED, IMPORTS, PLANFILE):
            (wd / name).unlink(missing_ok=True)
        files = adapter.provider_files(tool_versions(), state["account"])
        if deps.backend_hcl:
            files["backend.tf"] = deps.backend_hcl
        for name, text in files.items():
            write(name, text)
        write(IMPORTS, hcl.import_blocks(_active(state)))
        try:
            tf.init()
            tf.check_versions()
            tf.plan(out=PLANFILE, generate_config_out=GENERATED)
        except TerraformError as e:
            if not (wd / GENERATED).exists():  # config was not generated: nothing to repair
                result = GateResult(gate="generate", findings=[Finding(outcome=GateOutcome.FAIL, message=str(e))])
                return {"gates": _gate(state, result), "status": "failed"}
        return {"status": "running"}

    # --- 6. Static checks ----------------------------------------------------------
    def static(state: RunState) -> RunState:
        tf.fmt_write()
        files = {p.name: p.read_text() for p in sorted(wd.glob("*.tf"))}
        result = static_gate(files, tf.fmt_unformatted(), tf.validate(),
                             deps.scanners.tflint(wd), deps.scanners.gitleaks(wd))  # fmt: skip
        return _after_gate(state, result)

    # --- 7. Plan gate --------------------------------------------------------------------
    def plan(state: RunState) -> RunState:
        active = _active(state)
        planfile = tf.plan(out=PLANFILE)
        plan_json = tf.show_json(planfile)
        pg = plan_gate(plan_json, active, sha256_file(planfile))
        cg = config_gate(plan_json, active, adapter.inline_blocks, adapter.identity_attrs)
        merged = GateResult(gate="plan", findings=pg.findings + cg.findings, detail=pg.detail)
        update = _after_gate(state, merged)
        if merged.passed:
            update["plan_gate"] = merged.model_dump(mode="json")
        return update

    def _after_gate(state: RunState, result: GateResult) -> RunState:
        update: RunState = {"gates": _gate(state, result), "pending": []}
        if result.outcome is GateOutcome.HARD_STOP:
            update["status"] = "hard_stop"
        elif result.outcome in (GateOutcome.FAIL, GateOutcome.BLOCKED):
            update["status"] = "failed"
        elif result.outcome is GateOutcome.REPAIR:
            update["pending"] = [f.model_dump(mode="json") for f in result.findings if f.outcome is GateOutcome.REPAIR]
        return update

    # --- Repair loop: deterministic first, LLM second, skip after MAX_ATTEMPTS -------
    def repair(state: RunState) -> RunState:
        attempts, skipped = dict(state.get("attempts", {})), dict(state.get("skipped", {}))
        in_scope = {s.address for s in _active(state)}
        generated = read(GENERATED)
        by_address: dict[str, list[Finding]] = {}
        for f in (Finding(**d) for d in state.get("pending", [])):
            by_address.setdefault(f.address or "", []).append(f)

        for address, findings in sorted(by_address.items()):
            messages = [f.message + (f" {f.detail['changed']}" if f.detail.get("changed") else "") for f in findings]
            block = hcl.get_block(generated, address)
            if address not in in_scope:
                if block is not None:  # a resource nobody signed off: drop it
                    generated = hcl.remove_block(generated, address)
                continue
            if block is None:
                skipped[address] = "not in the generated configuration"
            elif any(f.detail.get("kind") == "secret" for f in findings):
                skipped[address] = "secret value in generated code; never sent to the LLM"
            elif attempts.get(address, 0) >= MAX_ATTEMPTS:
                skipped[address] = f"repair failed {MAX_ATTEMPTS} times: " + "; ".join(messages)
            elif all("reference" in f.detail for f in findings):  # hardcoded IDs only: rewritten in code
                attempts[address] = attempts.get(address, 0) + 1
                for f in findings:
                    block = hcl.use_reference(block, f.detail["id"], f"{f.detail['reference']}.id")
                generated = hcl.replace_block(generated, address, block)
            elif hcl.has_attribute(block, adapter.secret_attrs.get(address.split(".")[0], set())):
                skipped[address] = "needs LLM repair but holds a secret-bearing attribute; never sent"
            else:
                attempts[address] = attempts.get(address, 0) + 1
                if (fixed := repair_block(deps.repairer, address, block, messages)) is not None:
                    generated = hcl.replace_block(generated, address, fixed)
            if address in skipped and block is not None:
                generated = hcl.remove_block(generated, address)

        write(GENERATED, generated)
        update: RunState = {"attempts": attempts, "skipped": skipped, "pending": []}
        update["status"] = "running" if _active({**state, **update}) else "nothing_to_adopt"
        write(IMPORTS, hcl.import_blocks(_active({**state, **update})))
        return update

    # --- 8. Approve (human), then re-scan right before import ------------------------
    def approve(state: RunState) -> RunState:
        answer = interrupt({
            "step": "approve_plan",
            "plan_sha256": state["plan_gate"]["detail"]["plan_sha256"],
            "imports": [s.address for s in _active(state)],
            "skipped": state.get("skipped", {}),
        })  # fmt: skip
        return {"status": "running" if answer is True else "rejected"}

    def rescan(state: RunState) -> RunState:
        found = adapter.discover()
        if found.incomplete:
            return {"gates": _gate(state, coverage_gate(found.incomplete)), "status": "blocked"}
        keys = {(s.terraform_type, s.import_id) for s in _active(state)}
        before = {k: v for k, v in state["fingerprints"].items() if tuple(k.split(":", 1)) in keys}
        result = fingerprint_gate(before, found.fingerprints(keys))
        update: RunState = {"gates": _gate(state, result)}
        if not result.passed:
            restarts = state.get("restarts", 0) + 1
            update["restarts"] = restarts
            update["status"] = "restart" if restarts <= deps.max_restarts else "blocked"
        return update

    # --- 9. Import -------------------------------------------------------------------------
    def import_(state: RunState) -> RunState:
        backups = wd / "state-backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:  # backup first; an empty state pulls as empty text
            (backups / f"{state['run_id']}-{stamp}.tfstate").write_text(tf.state_pull())
        except TerraformError as e:
            failed = GateResult(gate="import", findings=[Finding(outcome=GateOutcome.FAIL, message=f"no backup: {e}")])
            return {"gates": _gate(state, failed), "status": "failed"}
        tf.apply(wd / PLANFILE, GateResult(**state["plan_gate"]))
        return {}

    # --- 10. Verify ----------------------------------------------------------------------
    def verify(state: RunState) -> RunState:
        active = _active(state)
        in_state = tf.state_list()
        result = verify_gate(tf.detailed_exitcode(), tf.detailed_exitcode(refresh_only=True), in_state, active)
        return {
            "gates": _gate(state, result),
            "adopted": sorted({s.address for s in active} & set(in_state)),
            "status": "adopted" if result.passed else "failed",
        }

    def report(state: RunState) -> RunState:
        found = Discovery(**state["discovery"]) if state.get("discovery") else None
        write("ADOPTION_REPORT.md", adoption_report(
            account=state["account"], region=state["region"], run_id=state["run_id"], versions=tool_versions(),
            classifications=[Classification(**c) for c in state.get("classifications", [])],
            scope=[ScopeItem(**s) for s in state.get("scope", [])], adopted=set(state.get("adopted", [])),
            skipped=state.get("skipped", {}), coverage=found.coverage if found else [],
            gates=[GateResult(**g) for g in state.get("gates", [])],
        ) + f"\n\nRun status: **{state.get('status')}**\n")  # fmt: skip
        if (wd / GENERATED).exists():
            write("FINDINGS.md", findings_report(deps.scanners.checkov(wd)))
        return {}

    # --- Wiring ------------------------------------------------------------------------------
    def go(ok: str) -> Callable[[RunState], str]:
        return lambda s: ok if s.get("status") == "running" else "report"

    def after_check(s: RunState) -> str:
        if s.get("status") != "running":
            return "report"
        return "repair" if s.get("pending") else "next"

    g = StateGraph(RunState)
    for name, fn in [("guard", guard), ("discover", discover), ("classify", classify),
                     ("scope_signoff", scope_signoff), ("generate", generate), ("static", static),
                     ("plan", plan), ("repair", repair), ("approve", approve), ("rescan", rescan),
                     ("import", import_), ("verify", verify), ("report", report)]:  # fmt: skip
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", go("discover"))
    g.add_conditional_edges("discover", go("classify"))
    g.add_edge("classify", "scope_signoff")
    g.add_conditional_edges("scope_signoff", go("generate"))
    g.add_conditional_edges("generate", go("static"))
    g.add_conditional_edges("static", after_check, {"repair": "repair", "next": "plan", "report": "report"})
    g.add_conditional_edges("plan", after_check, {"repair": "repair", "next": "approve", "report": "report"})
    g.add_conditional_edges("repair", go("static"))
    g.add_conditional_edges("approve", go("rescan"))
    g.add_conditional_edges(
        "rescan",
        lambda s: {"running": "import", "restart": "discover"}.get(s.get("status", ""), "report"),
        {"import": "import", "discover": "discover", "report": "report"},
    )
    g.add_conditional_edges("import", go("verify"))
    g.add_edge("verify", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def start_state(run_id: str, account: str, region: str) -> RunState:
    return {"run_id": run_id, "account": account, "region": region, "status": "running", "gates": [], "restarts": 0}
