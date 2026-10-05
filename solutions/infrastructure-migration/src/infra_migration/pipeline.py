"""Governed coordinator of focused LangGraph specialists; execution stays outside this graph."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .generation import ProjectBundle, verify_bundle_directory
from .models import AssessmentPlan, Inventory, Principal, ReviewDecision, Scope, digest
from .reasoning import ModelProposal
from .specialists import (
    ORDER,
    VERSION,
    AssessmentSpecialist,
    DiscoveryRequest,
    DiscoverySpecialist,
    GenerationRequest,
    GenerationSpecialist,
    MigrationPlan,
    PlanningRequest,
    PlanningSpecialist,
    ReasoningRequest,
    ReasoningSpecialist,
    RecoveryRequest,
    RecoverySpecialist,
    ValidationRequest,
    ValidationSpecialist,
    bind_handoff,
    check_handoffs,
)
from .tools import AccessDenied


class PipelineState(TypedDict, total=False):
    architecture_version: str
    handoffs_json: str
    inventory_json: str
    selected_ids: tuple[str, ...]
    assessment_json: str
    proposal_json: str
    model_receipt: dict
    migration_plan_json: str
    recovery_plan_json: str
    bundle_json: str
    compile_receipt: dict
    package_digest: str
    status: str


class MigrationPipeline:
    def __init__(
        self,
        principal: Principal,
        scope: Scope,
        discover,
        checkpointer,
        *,
        model_reviewer=None,
        documents=(),
        runner=None,
        output_directory=None,
    ):
        if principal.tenant_id != scope.tenant_id or "assessor" not in principal.roles:
            raise AccessDenied("Pipeline authorization failed")
        self.principal, self.scope = principal, scope
        self.config = {"configurable": {"thread_id": f"pipeline:{scope.tenant_id}:{scope.run_id}"}}
        self._legacy = None
        saved = checkpointer.get_tuple(self.config)
        if saved is not None:
            channels = saved.checkpoint["channel_values"]
            version = channels.get("architecture_version")
            if version is None and isinstance(channels.get("__start__"), dict):
                version = channels["__start__"].get("architecture_version")
            if version is None:
                from .legacy_pipeline import LegacyMigrationPipeline

                self._legacy = LegacyMigrationPipeline(
                    principal,
                    scope,
                    discover,
                    checkpointer,
                    model_reviewer=model_reviewer,
                    documents=documents,
                    runner=runner,
                    output_directory=output_directory,
                )
                self.graph = self._legacy.graph
                return
            if version != VERSION:
                raise AccessDenied("Checkpoint workflow version is not supported")

        specialists = {
            "discover": DiscoverySpecialist(discover),
            "assess": AssessmentSpecialist(),
            "reason": ReasoningSpecialist(model_reviewer, documents),
            "plan": PlanningSpecialist(),
            "recover": RecoverySpecialist(),
            "generate": GenerationSpecialist(),
            "validate": ValidationSpecialist(output_directory, runner),
        }

        def inventory(state):
            observed = Inventory.model_validate_json(state["inventory_json"])
            if observed.scope != scope:
                raise AccessDenied("Specialist inventory changed scope")
            return observed

        def plan(state):
            result = MigrationPlan.model_validate_json(state["migration_plan_json"])
            if result.scope != scope or result.selected_ids != tuple(state["selected_ids"]):
                raise AccessDenied("Specialist plan changed selection")
            return result

        def dispatch(name, state):
            if state.get("architecture_version") != VERSION:
                raise AccessDenied("Specialist workflow version changed")
            check_handoffs(state, scope)
            if name == "discover":
                response = specialists[name].run(DiscoveryRequest(scope=scope))
                output = {"inventory_json": response.model_dump_json()}
            elif name == "assess":
                response = specialists[name].run(inventory(state))
                output = {"assessment_json": response.model_dump_json()}
            elif name == "reason":
                response = specialists[name].run(ReasoningRequest(inventory=inventory(state)))
                output = {
                    "proposal_json": response.proposal.model_dump_json()
                    if response.proposal
                    else "",
                    "model_receipt": response.receipt,
                }
            elif name == "plan":
                response = specialists[name].run(
                    PlanningRequest(
                        inventory=inventory(state),
                        assessment=AssessmentPlan.model_validate_json(state["assessment_json"]),
                        selected_ids=tuple(state["selected_ids"]),
                        proposal=ModelProposal.model_validate_json(state["proposal_json"])
                        if state["proposal_json"]
                        else None,
                    )
                )
                output = {"migration_plan_json": response.model_dump_json()}
            elif name == "recover":
                response = specialists[name].run(
                    RecoveryRequest(inventory=inventory(state), plan=plan(state))
                )
                output = {"recovery_plan_json": response.model_dump_json()}
            elif name == "generate":
                response = specialists[name].run(
                    GenerationRequest(inventory=inventory(state), plan=plan(state))
                )
                output = {"bundle_json": response.model_dump_json()}
            else:
                response = specialists[name].run(
                    ValidationRequest(
                        bundle=ProjectBundle.model_validate_json(state["bundle_json"]),
                    )
                )
                output = {"compile_receipt": response.compile_receipt}
            result = bind_handoff(name, output, state, scope)
            if name == "validate":
                result["status"] = "AWAITING_PACKAGE_REVIEW"
            return result

        def review(state):
            check_handoffs(state, scope, complete=True)
            if output_directory is None:
                raise AccessDenied("Verifier output directory required")
            verify_bundle_directory(
                ProjectBundle.model_validate_json(state["bundle_json"]), output_directory
            )
            package = {
                k: state[k]
                for k in (
                    "architecture_version",
                    "handoffs_json",
                    "assessment_json",
                    "proposal_json",
                    "model_receipt",
                    "migration_plan_json",
                    "recovery_plan_json",
                    "bundle_json",
                    "compile_receipt",
                )
            }
            expected = digest(package)
            response = ReviewDecision.model_validate(
                interrupt(
                    {
                        "purpose": "Review specialist package; execution needs separate approval",
                        "package": package,
                        "plan_digest": expected,
                    }
                )
            )
            if response.plan_digest != expected:
                raise AccessDenied("Review does not bind this package")
            return {
                "package_digest": expected,
                "status": "REVIEWED_EXECUTION_BLOCKED" if response.acknowledged else "REJECTED",
            }

        graph = StateGraph(PipelineState)
        for name in ORDER:
            # Bind name per wrapper; each invokes its own private typed specialist graph.
            def node(state, specialist=name):
                return dispatch(specialist, state)

            graph.add_node(name, node)
        graph.add_node("review", review)
        graph.add_edge(START, ORDER[0])
        for source, target in zip(ORDER, ORDER[1:], strict=False):
            graph.add_edge(source, target)
        graph.add_edge(ORDER[-1], "review")
        graph.add_edge("review", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def _snapshot(self):
        if self._legacy is not None:
            return self._legacy._snapshot()
        state = self.graph.get_state(self.config)
        if state.values:
            if state.values.get("architecture_version") != VERSION:
                raise AccessDenied("Checkpoint workflow version changed")
            check_handoffs(state.values, self.scope)
            if "inventory_json" in state.values:
                inventory = Inventory.model_validate_json(state.values["inventory_json"])
                if inventory.scope != self.scope:
                    raise AccessDenied("Pipeline checkpoint scope changed")
        return state

    def start(self, selected_ids: tuple[str, ...]):
        if self._legacy is not None:
            return self._legacy.start(selected_ids)
        if (
            not selected_ids
            or len(selected_ids) > 1000
            or len(set(selected_ids)) != len(selected_ids)
        ):
            raise AccessDenied("Select between one and 1000 unique resources")
        if self._snapshot().values:
            raise AccessDenied("Pipeline run already exists")
        return self.graph.invoke(
            {
                "architecture_version": VERSION,
                "handoffs_json": "[]",
                "selected_ids": selected_ids,
            },
            self.config,
        )

    def resume_review(self, principal: Principal, decision: ReviewDecision):
        if self._legacy is not None:
            return self._legacy.resume_review(principal, decision)
        if principal.tenant_id != self.scope.tenant_id or "reviewer" not in principal.roles:
            raise AccessDenied("Pipeline review authorization failed")
        state = self._snapshot()
        if state.next != ("review",):
            raise AccessDenied("Pipeline is not awaiting review")
        check_handoffs(state.values, self.scope, complete=True)
        return self.graph.invoke(Command(resume=decision.model_dump()), self.config)
