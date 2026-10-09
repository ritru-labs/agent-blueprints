"""LangGraph flow: discover -> scaffold -> plan <-> repair -> human review -> import -> verify.

discover ─► scaffold ─► plan ──zero change──► review ──approved──► import ─► END
                         ▲  │                    │
                         │  └─diff, budget left─► repair
                         └──────────────────────┘
          plan ──diff, budget spent──► stuck ─► END       review ──rejected──► END
"""

from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .discover import Discovery
from .repair import RepairUnavailable
from .terraform import UnsafeEdit, Workspace


class State(TypedDict, total=False):
    project: str
    resources: list[dict]
    skipped: list[str]
    plan: dict
    attempts: int
    feedback: Annotated[list[str], add]  # what each repair attempt did; fed back to the model
    approved_sha: str
    status: str
    message: str


def build_graph(
    *,
    workspace: Workspace,
    discover,  # (project, asset_types) -> Discovery
    repairer,  # (config, plan_summary, feedback) -> RepairPlan
    checkpointer,
    asset_types: list[str] | None = None,
    provider_version: str = ">= 6.0",
    max_attempts: int = 3,
    state_bucket: str | None = None,
):
    def discover_node(state: State):
        found: Discovery = discover(state["project"], asset_types)
        if not found.resources:
            return {"resources": [], "skipped": found.skipped, "status": "NOTHING_TO_IMPORT"}
        return {"resources": [r.to_dict() for r in found.resources], "skipped": found.skipped}

    def scaffold_node(state: State):
        workspace.scaffold(state["project"], state["resources"], provider_version, state_bucket)
        workspace.generate_config()
        return {"attempts": 0}

    def plan_node(state: State):
        return {"plan": workspace.plan(len(state["resources"])).summary()}

    def repair_node(state: State):
        attempt = state["attempts"] + 1
        try:
            fix = repairer(workspace.generated(), state["plan"], state.get("feedback", []))
        except RepairUnavailable as exc:
            return {"attempts": max_attempts, "feedback": [f"attempt {attempt}: {exc}"]}
        if not fix.edits:
            # The model says the rest cannot be fixed in configuration; re-planning would not change anything.
            return {"attempts": max_attempts, "feedback": [f"attempt {attempt}: no edits. {fix.notes}"]}
        try:
            allowed = {r["tf_type"] for r in state["resources"]}
            workspace.apply_edits([e.model_dump() for e in fix.edits], allowed)
        except UnsafeEdit as exc:
            return {
                "attempts": attempt,
                "feedback": [f"attempt {attempt}: edits rejected ({exc}). {fix.notes}"],
            }
        return {
            "attempts": attempt,
            "feedback": [f"attempt {attempt}: applied {len(fix.edits)} edits. {fix.notes}"],
        }

    def stuck_node(state: State):
        return {
            "status": "NEEDS_HUMAN",
            "message": "Could not reach a zero-change plan. Fix generated.tf by hand, then re-run with --replan.",
        }

    def review_node(state: State):
        # No side effects before interrupt(): LangGraph re-runs this node when resuming.
        decision = interrupt(
            {
                "resources": [
                    f"{r['tf_type']}.{r['tf_name']} <- {r['import_id']}" for r in state["resources"]
                ],
                "skipped": state.get("skipped", []),
                "repair_log": state.get("feedback", []),
                "plan_sha256": state["plan"]["plan_sha256"],
            }
        )
        if not decision.get("approve"):
            return {"status": "REJECTED", "message": "Reviewer rejected the import."}
        if decision.get("plan_sha256") != state["plan"]["plan_sha256"]:
            return {"status": "REJECTED", "message": "Approval does not match the reviewed plan."}
        return {"approved_sha": decision["plan_sha256"]}

    def import_node(state: State):
        workspace.import_reviewed_plan(state["approved_sha"], len(state["resources"]))
        if workspace.verify_no_changes():
            return {"status": "DONE", "message": "Imported; terraform plan now reports no changes."}
        return {"status": "DRIFTED", "message": "Imported, but a fresh plan shows changes: the cloud moved."}

    def after_plan(state: State):
        if state["plan"]["zero_change"]:
            return "review"
        return "repair" if state["attempts"] < max_attempts else "stuck"

    graph = StateGraph(State)
    for name, node in [
        ("discover", discover_node),
        ("scaffold", scaffold_node),
        ("plan", plan_node),
        ("repair", repair_node),
        ("stuck", stuck_node),
        ("review", review_node),
        ("import", import_node),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "discover")
    graph.add_conditional_edges("discover", lambda s: END if not s["resources"] else "scaffold")
    graph.add_edge("scaffold", "plan")
    graph.add_conditional_edges("plan", after_plan, ["review", "repair", "stuck"])
    graph.add_edge("repair", "plan")
    graph.add_edge("stuck", END)
    graph.add_conditional_edges("review", lambda s: "import" if s.get("approved_sha") else END)
    graph.add_edge("import", END)
    return graph.compile(checkpointer=checkpointer)
