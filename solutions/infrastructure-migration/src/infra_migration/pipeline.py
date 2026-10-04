"""LangGraph discovery-to-review pipeline with optional model reasoning and isolated compilation."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .assessment import assess
from .generation import ProjectBundle, generate, write_bundle
from .models import Inventory, Principal, ReviewDecision, Scope, digest
from .reasoning import ModelProposal
from .tools import AccessDenied


class PipelineState(TypedDict, total=False):
    inventory_json: str
    selected_ids: tuple[str, ...]
    assessment_json: str
    proposal_json: str
    model_receipt: dict
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

        def discovery(state):
            inventory = discover()
            if inventory.scope != scope:
                raise AccessDenied("Discovery returned a different scope")
            return {
                "inventory_json": inventory.model_dump_json(),
                "assessment_json": assess(inventory).model_dump_json(),
            }

        def reasoning(state):
            if model_reviewer is None:
                return {"proposal_json": "", "model_receipt": {}, "status": "MODEL_NOT_CONFIGURED"}
            inventory = Inventory.model_validate_json(state["inventory_json"])
            proposal = model_reviewer.review(inventory, documents)
            return {
                "proposal_json": proposal.model_dump_json(),
                "model_receipt": model_reviewer.last_receipt,
            }

        def generation(state):
            inventory = Inventory.model_validate_json(state["inventory_json"])
            if state.get("proposal_json"):
                proposal = ModelProposal.model_validate_json(state["proposal_json"])
                aliases = {
                    f"resource-{i}": r.resource_id for i, r in enumerate(inventory.resources)
                }
                if any(
                    r.disposition == "blocked"
                    and aliases[r.resource_alias] in state["selected_ids"]
                    for r in proposal.recommendations
                ):
                    raise AccessDenied("Model review blocked a selected resource")
            bundle = generate(inventory, tuple(state["selected_ids"]))
            return {"bundle_json": bundle.model_dump_json()}

        def validation(state):
            bundle = ProjectBundle.model_validate_json(state["bundle_json"])
            if output_directory is None:
                raise AccessDenied("Verifier output directory required")
            if output_directory.exists():
                from .generation import verify_bundle_directory

                verify_bundle_directory(bundle, output_directory)
            else:
                write_bundle(bundle, output_directory)
            if runner is None:
                return {
                    "compile_receipt": {"passed": False, "reason": "RUNNER_NOT_CONFIGURED"},
                    "status": "AWAITING_PACKAGE_REVIEW",
                }
            return {
                "compile_receipt": runner.compile(bundle, output_directory),
                "status": "AWAITING_PACKAGE_REVIEW",
            }

        def review(state):
            from .generation import verify_bundle_directory

            if output_directory is not None:
                verify_bundle_directory(
                    ProjectBundle.model_validate_json(state["bundle_json"]), output_directory
                )
            package = {
                k: state[k]
                for k in (
                    "assessment_json",
                    "proposal_json",
                    "model_receipt",
                    "bundle_json",
                    "compile_receipt",
                )
            }
            expected = digest(package)
            response = ReviewDecision.model_validate(
                interrupt(
                    {
                        "purpose": "Review artifacts; live execution needs separate approval",
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
        for name, node in (
            ("discover", discovery),
            ("reason", reasoning),
            ("generate", generation),
            ("validate", validation),
            ("review", review),
        ):
            graph.add_node(name, node)
        graph.add_edge(START, "discover")
        for source, target in (
            ("discover", "reason"),
            ("reason", "generate"),
            ("generate", "validate"),
            ("validate", "review"),
        ):
            graph.add_edge(source, target)
        graph.add_edge("review", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def _snapshot(self):
        state = self.graph.get_state(self.config)
        if state.values:
            inventory = Inventory.model_validate_json(state.values["inventory_json"])
            if inventory.scope != self.scope:
                raise AccessDenied("Pipeline checkpoint scope changed")
        return state

    def start(self, selected_ids: tuple[str, ...]):
        if self._snapshot().values:
            raise AccessDenied("Pipeline run already exists")
        return self.graph.invoke({"selected_ids": selected_ids}, self.config)

    def resume_review(self, principal: Principal, decision: ReviewDecision):
        if principal.tenant_id != self.scope.tenant_id or "reviewer" not in principal.roles:
            raise AccessDenied("Pipeline review authorization failed")
        state = self._snapshot()
        if not state.next or state.next != ("review",):
            raise AccessDenied("Pipeline is not awaiting review")
        return self.graph.invoke(Command(resume=decision.model_dump()), self.config)
