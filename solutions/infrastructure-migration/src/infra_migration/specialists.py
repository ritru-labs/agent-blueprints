"""Focused, stateless LangGraph specialists with typed inputs and fixed tool surfaces.

These components cannot authorize cloud changes, select credentials, or admit adapters.
Only ReasoningSpecialist may invoke a model, through the existing bounded reviewer.
"""

import json
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from .assessment import assess
from .generation import ProjectBundle, generate, verify_bundle_directory, write_bundle
from .models import AssessmentPlan, Contract, Inventory, Scope, digest
from .reasoning import ModelProposal
from .tools import AccessDenied

VERSION = "specialists-v1"
ORDER = ("discover", "assess", "reason", "plan", "recover", "generate", "validate")
FIELDS = {
    "discover": ("inventory_json",),
    "assess": ("assessment_json",),
    "reason": ("proposal_json", "model_receipt"),
    "plan": ("migration_plan_json",),
    "recover": ("recovery_plan_json",),
    "generate": ("bundle_json",),
    "validate": ("compile_receipt",),
}


class Handoff(Contract):
    specialist: Literal["discover", "assess", "reason", "plan", "recover", "generate", "validate"]
    version: Literal["specialists-v1"] = VERSION
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_enabled: Literal[False] = False


def seed(scope, selected):
    return digest({"scope": scope.model_dump(mode="json"), "selected_ids": list(selected)})


def check_handoffs(state, scope, *, complete=False):
    try:
        raw = json.loads(state.get("handoffs_json", "[]"))
        if not isinstance(raw, list) or len(raw) > len(ORDER):
            raise ValueError("Invalid handoff count")
        if complete and len(raw) != len(ORDER):
            raise ValueError("Incomplete specialist results")
        previous = seed(scope, state["selected_ids"])
        receipts = []
        for expected, item in zip(ORDER, raw, strict=False):
            receipt = Handoff.model_validate_json(json.dumps(item))
            output = {key: state[key] for key in FIELDS[expected]}
            if (
                receipt.specialist != expected
                or receipt.scope_digest != digest(scope)
                or receipt.input_digest != previous
                or receipt.output_digest != digest(output)
            ):
                raise ValueError("Handoff binding mismatch")
            receipts.append(receipt)
            previous = digest(receipt)
        return receipts
    except (ValueError, KeyError, TypeError):
        raise AccessDenied("Specialist handoff failed verification") from None


def bind_handoff(name, output, state, scope):
    receipts = check_handoffs(state, scope)
    if len(receipts) >= len(ORDER) or ORDER[len(receipts)] != name:
        raise AccessDenied("Specialist invoked out of order")
    if set(output) != set(FIELDS[name]):
        raise AccessDenied("Specialist attempted to write outside its contract")
    receipt = Handoff(
        specialist=name,
        scope_digest=digest(scope),
        input_digest=digest(receipts[-1]) if receipts else seed(scope, state["selected_ids"]),
        output_digest=digest(output),
    )
    return {
        **output,
        "handoffs_json": json.dumps([r.model_dump(mode="json") for r in (*receipts, receipt)]),
    }


class SpecialistState(TypedDict):
    request_json: str
    response_json: str


class Specialist:
    """A private stateless subgraph; no supervisor state or unrestricted tool router."""

    def __init__(self, request_type, response_type, tool):
        self.request_type, self.response_type = request_type, response_type

        def act(state):
            request = request_type.model_validate_json(state["request_json"])
            response = tool(request)
            if not isinstance(response, response_type):
                raise AccessDenied("Specialist returned an invalid contract")
            return {"response_json": response.model_dump_json()}

        graph = StateGraph(SpecialistState)
        graph.add_node("act", act)
        graph.add_edge(START, "act")
        graph.add_edge("act", END)
        self.graph = graph.compile(checkpointer=False)

    def run(self, request):
        if not isinstance(request, self.request_type):
            raise AccessDenied("Specialist received an invalid contract")
        result = self.graph.invoke({"request_json": request.model_dump_json()})
        return self.response_type.model_validate_json(result["response_json"])


class DiscoveryRequest(Contract):
    scope: Scope


class DiscoverySpecialist(Specialist):
    def __init__(self, discover):
        def tool(request):
            inventory = discover()
            if not isinstance(inventory, Inventory) or inventory.scope != request.scope:
                raise AccessDenied("Discovery returned a different scope")
            return inventory

        super().__init__(DiscoveryRequest, Inventory, tool)


class AssessmentSpecialist(Specialist):
    def __init__(self):
        super().__init__(Inventory, AssessmentPlan, assess)


class ReasoningRequest(Contract):
    inventory: Inventory


class ReasoningResult(Contract):
    proposal: ModelProposal | None = None
    receipt: dict = Field(default_factory=dict)


class ReasoningSpecialist(Specialist):
    def __init__(self, reviewer=None, documents=()):
        def tool(request):
            if reviewer is None:
                return ReasoningResult()
            proposal = reviewer.review(request.inventory, documents)
            return ReasoningResult(proposal=proposal, receipt=reviewer.last_receipt)

        super().__init__(ReasoningRequest, ReasoningResult, tool)


class PlanningRequest(Contract):
    inventory: Inventory
    assessment: AssessmentPlan
    selected_ids: tuple[str, ...] = Field(min_length=1, max_length=1000)
    proposal: ModelProposal | None = None


class MigrationPlan(Contract):
    scope: Scope
    inventory_digest: str
    selected_ids: tuple[str, ...]
    dependency_waves: tuple[tuple[str, ...], ...]
    blockers: tuple[str, ...]
    execution_enabled: Literal[False] = False


class PlanningSpecialist(Specialist):
    def __init__(self):
        def tool(request):
            inventory, assessment, selected = (
                request.inventory,
                request.assessment,
                request.selected_ids,
            )
            if assessment != assess(inventory):
                raise AccessDenied("Assessment does not bind the observed inventory")
            if len(set(selected)) != len(selected):
                raise AccessDenied("Selection must contain unique resources")
            known = {r.resource_id: r for r in inventory.resources}
            if any(r not in known for r in selected):
                raise AccessDenied("Planning contains undiscovered resources")
            if request.proposal is not None:
                aliases = {
                    f"resource-{i}": r.resource_id for i, r in enumerate(inventory.resources)
                }
                for recommendation in request.proposal.recommendations:
                    if recommendation.resource_alias not in aliases:
                        raise AccessDenied("Model invented a resource")
                    if (
                        recommendation.disposition == "blocked"
                        and aliases[recommendation.resource_alias] in selected
                    ):
                        raise AccessDenied("Model review blocked a selected resource")
            remaining, done, waves = set(selected), set(), []
            while remaining:
                ready = sorted(r for r in remaining if set(known[r].dependencies) <= done)
                if not ready:
                    raise AccessDenied("Selection has missing or cyclic dependencies")
                waves.append(tuple(ready))
                done.update(ready)
                remaining.difference_update(ready)
            blockers = set(assessment.blockers) | set(inventory.gaps)
            for row in assessment.assessments:
                if row.resource_id in selected:
                    blockers.update(row.blockers)
            return MigrationPlan(
                scope=inventory.scope,
                inventory_digest=digest(inventory),
                selected_ids=selected,
                dependency_waves=tuple(waves),
                blockers=tuple(sorted(blockers)),
            )

        super().__init__(PlanningRequest, MigrationPlan, tool)


class RecoveryRequest(Contract):
    inventory: Inventory
    plan: MigrationPlan


class RecoveryAction(Contract):
    resource_id: str
    ownership: Literal["manual", "cloudformation", "unknown"]
    resource_type: str
    required_observations: tuple[str, ...]
    before_transfer: str
    after_transfer: str
    status: Literal["requires_live_qualification"] = "requires_live_qualification"


class RecoveryPlan(Contract):
    scope: Scope
    plan_digest: str
    actions: tuple[RecoveryAction, ...]
    qualified: Literal[False] = False
    execution_enabled: Literal[False] = False


class RecoverySpecialist(Specialist):
    def __init__(self):
        def tool(request):
            inventory, plan = request.inventory, request.plan
            if plan.scope != inventory.scope or plan.inventory_digest != digest(inventory):
                raise AccessDenied("Recovery plan inventory binding changed")
            known = {r.resource_id: r for r in inventory.resources}
            actions = []
            for identity in plan.selected_ids:
                if identity not in known:
                    raise AccessDenied("Recovery resource was not observed")
                resource = known[identity]
                before = (
                    (
                        "Keep CloudFormation ownership; verify retention before release. "
                        "Stop on uncertain source status."
                    )
                    if resource.owner == "cloudformation"
                    else (
                        "Keep existing ownership; perform no cloud writes. "
                        "Resolve unknown ownership before adoption."
                    )
                )
                actions.append(
                    RecoveryAction(
                        resource_id=identity,
                        ownership=resource.owner,
                        resource_type=resource.resource_type,
                        required_observations=(
                            (
                                "physical VPC ID and CIDR",
                                "DNS attributes",
                                "dependent subnet identities",
                            )
                            if resource.resource_type == "AWS::EC2::VPC"
                            else (
                                "physical subnet ID and CIDR",
                                "parent VPC identity",
                                "availability zone and IP settings",
                            )
                            if resource.resource_type == "AWS::EC2::Subnet"
                            else ("unsupported resource: stop until a qualified adapter exists",)
                        ),
                        before_transfer=before,
                        after_transfer=(
                            "Stop writes; reconcile resource identity and ownership state. "
                            "State restore alone is not rollback. Do not delete resources or "
                            "auto-re-adopt; require qualified recovery and approval."
                        ),
                    )
                )
            return RecoveryPlan(
                scope=inventory.scope, plan_digest=digest(plan), actions=tuple(actions)
            )

        super().__init__(RecoveryRequest, RecoveryPlan, tool)


class GenerationRequest(Contract):
    inventory: Inventory
    plan: MigrationPlan


class GenerationSpecialist(Specialist):
    def __init__(self):
        def tool(request):
            if (
                request.plan.scope != request.inventory.scope
                or request.plan.inventory_digest != digest(request.inventory)
            ):
                raise AccessDenied("Generation plan binding changed")
            return generate(request.inventory, request.plan.selected_ids)

        super().__init__(GenerationRequest, ProjectBundle, tool)


class ValidationRequest(Contract):
    bundle: ProjectBundle


class ValidationResult(Contract):
    compile_receipt: dict


class ValidationSpecialist(Specialist):
    def __init__(self, directory: Path | None, runner=None):
        def tool(request):
            if directory is None:
                raise AccessDenied("Verifier output directory required")
            if directory.exists():
                verify_bundle_directory(request.bundle, directory)
            else:
                write_bundle(request.bundle, directory)
            receipt = (
                runner.compile(request.bundle, directory)
                if runner
                else {
                    "passed": False,
                    "reason": "RUNNER_NOT_CONFIGURED",
                }
            )
            verify_bundle_directory(request.bundle, directory)
            return ValidationResult(compile_receipt=receipt)

        super().__init__(ValidationRequest, ValidationResult, tool)
