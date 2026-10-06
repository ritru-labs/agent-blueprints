"""Durable LangGraph assessment and review with authorization on every entry point."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .assessment import assess
from .models import AssessmentPlan, Inventory, Principal, ReviewDecision, Scope, digest
from .tools import AccessDenied, FixtureGateway, ToolRequest


class State(TypedDict, total=False):
    inventory_json: str
    receipt_digest: str
    plan_json: str
    status: str
    reviewer: str


def assess_node(state: State):
    inventory = Inventory.model_validate_json(state["inventory_json"])
    if digest(inventory) != state["receipt_digest"]:
        raise AccessDenied("Inventory receipt mismatch")
    return {"plan_json": assess(inventory).model_dump_json(), "status": "AWAITING_REVIEW"}


def review_node(state: State):
    plan = AssessmentPlan.model_validate_json(state["plan_json"])
    # No side effects before interrupt: LangGraph reruns this node when resuming.
    decision = ReviewDecision.model_validate(
        interrupt(
            {
                "purpose": "Acknowledge assessment only; infrastructure writes remain disabled",
                "plan": plan.model_dump(mode="json"),
                "plan_digest": digest(plan),
            }
        )
    )
    if decision.plan_digest != digest(plan):
        raise AccessDenied("Review decision does not match the exact plan")
    return {"status": "REVIEWED_BLOCKED" if decision.acknowledged else "REVIEW_REJECTED"}


def build_graph(checkpointer):
    graph = StateGraph(State)
    graph.add_node("assess", assess_node)
    graph.add_node("review", review_node)
    graph.add_edge(START, "assess")
    graph.add_edge("assess", "review")
    graph.add_edge("review", END)
    return graph.compile(checkpointer=checkpointer)


class AssessmentService:
    """Local application facade. Do not expose raw graph or checkpointer to clients."""

    def __init__(self, gateway: FixtureGateway, checkpointer):
        self._gateway = gateway
        self._graph = build_graph(checkpointer)

    @staticmethod
    def _authorize(principal: Principal, scope: Scope, role: str):
        if principal.tenant_id != scope.tenant_id or role not in principal.roles:
            raise AccessDenied("Role and tenant authorization failed")

    @staticmethod
    def _config(scope: Scope):
        # A namespace prevents ordinary cross-tenant thread collisions; it is not database RLS.
        return {"configurable": {"thread_id": f"{scope.tenant_id}:{scope.run_id}"}}

    def _snapshot(self, scope: Scope):
        snapshot = self._graph.get_state(self._config(scope))
        if snapshot.values:
            inventory = Inventory.model_validate_json(snapshot.values["inventory_json"])
            if inventory.scope != scope:
                raise AccessDenied("Checkpoint scope mismatch")
        return snapshot

    def start(self, principal: Principal, scope: Scope):
        self._authorize(principal, scope, "assessor")
        if self._snapshot(scope).values:
            raise AccessDenied("Run already exists; use a new run identity")
        inventory, receipt = self._gateway.invoke(
            principal,
            ToolRequest(name="read_inventory", scope=scope, request_id=f"{scope.run_id}:inventory"),
        )
        return self._graph.invoke(
            {
                "inventory_json": inventory.model_dump_json(),
                "receipt_digest": receipt.result_digest,
            },
            self._config(scope),
        )

    def read(self, principal: Principal, scope: Scope):
        if not any(role in principal.roles for role in ("assessor", "reviewer")):
            raise AccessDenied("Read role required")
        if principal.tenant_id != scope.tenant_id:
            raise AccessDenied("Tenant mismatch")
        return self._snapshot(scope).values

    def review(self, principal: Principal, scope: Scope, decision: ReviewDecision):
        self._authorize(principal, scope, "reviewer")
        snapshot = self._snapshot(scope)
        if not snapshot.values or snapshot.values.get("status") != "AWAITING_REVIEW":
            raise AccessDenied("Run is not awaiting review")
        plan = AssessmentPlan.model_validate_json(snapshot.values["plan_json"])
        if decision.plan_digest != digest(plan):
            raise AccessDenied("Stale or modified assessment decision")
        # Attribution comes from authenticated context, not interrupt input.
        self._graph.update_state(self._config(scope), {"reviewer": principal.subject})
        return self._graph.invoke(Command(resume=decision.model_dump()), self._config(scope))
