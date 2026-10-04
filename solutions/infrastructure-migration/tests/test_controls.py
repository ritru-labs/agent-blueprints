import json
from datetime import datetime
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError

from infra_migration.assessment import assess
from infra_migration.demo import fixture
from infra_migration.models import (
    AssessmentPlan,
    Inventory,
    Principal,
    ReviewDecision,
    Scope,
    digest,
)
from infra_migration.tools import AccessDenied, FixtureGateway, ToolRequest
from infra_migration.workflow import AssessmentService


def principal(inventory, roles=("assessor", "reviewer")):
    return Principal(tenant_id=inventory.scope.tenant_id, subject="trusted-user", roles=roles)


def changed_inventory(inventory, **changes):
    data = inventory.model_dump(mode="json")
    data.update(changes)

    return Inventory.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("name", ["import_resources", "apply", "release_ownership"])
def test_privileged_tools_always_denied(name):
    inventory = fixture()
    with pytest.raises(AccessDenied, match="disabled"):
        FixtureGateway(inventory).invoke(
            principal(inventory),
            ToolRequest(name=name, scope=inventory.scope, request_id="attempt"),
        )


def test_no_arbitrary_shell_or_unknown_tool():
    with pytest.raises(ValidationError):
        ToolRequest(name="shell", scope=fixture().scope, request_id="attempt")


def test_cross_tenant_inventory_access_denied():
    inventory = fixture()
    attacker = Principal(tenant_id=uuid4(), subject="attacker", roles=("assessor",))
    with pytest.raises(AccessDenied):
        FixtureGateway(inventory).invoke(
            attacker,
            ToolRequest(name="read_inventory", scope=inventory.scope, request_id="attempt"),
        )


def test_wrong_account_scope_denied():
    inventory = fixture()
    data = inventory.scope.model_dump()
    data["account_id"] = "111111111111"
    with pytest.raises(AccessDenied, match="exact"):
        FixtureGateway(inventory).invoke(
            principal(inventory),
            ToolRequest(name="read_inventory", scope=Scope(**data), request_id="attempt"),
        )


@pytest.mark.parametrize("change", ["account", "region", "duplicate", "timestamp"])
def test_invalid_inventory_is_rejected(change):
    inventory = fixture()
    data = inventory.model_dump(mode="json")
    if change == "account":
        data["resources"][0]["account_id"] = "111111111111"
    elif change == "region":
        data["resources"][0]["region"] = "eu-west-1"
    elif change == "duplicate":
        data["resources"].append(data["resources"][0])
    else:
        data["observed_at"] = datetime(2026, 10, 4).isoformat()
    with pytest.raises(ValidationError):
        changed_inventory(inventory, **data)


def test_assessment_never_claims_execution_or_complete_discovery():
    plan = assess(fixture())
    assert not plan.execution_enabled
    assert "DISCOVERY_INCOMPLETE" in plan.blockers
    assert "LIVE_ADAPTERS_NOT_QUALIFIED" in plan.blockers
    unknown = next(a for a in plan.assessments if a.resource_id == "unsupported-fixture")
    assert "UNSUPPORTED_RESOURCE_TYPE" in unknown.blockers
    assert "OWNERSHIP_UNKNOWN" in unknown.blockers


@pytest.mark.parametrize("workflow", ["manual_adoption", "cloudformation_migration"])
def test_workflow_owner_checked(workflow):
    inventory = fixture(workflow)
    plan = assess(inventory)
    vpc = next(a for a in plan.assessments if a.resource_id == "vpc-fixture")
    assert "OWNERSHIP_TRANSFER_REQUIRES_REVIEW" not in vpc.blockers
    data = inventory.model_dump(mode="json")
    data["resources"][0]["owner"] = "cloudformation" if workflow == "manual_adoption" else "manual"
    changed = assess(changed_inventory(inventory, **data))
    vpc = next(a for a in changed.assessments if a.resource_id == "vpc-fixture")
    assert "OWNERSHIP_TRANSFER_REQUIRES_REVIEW" in vpc.blockers


def test_dependency_cycle_and_missing_dependency_blocked():
    inventory = fixture()
    data = inventory.model_dump(mode="json")
    data["resources"][0]["dependencies"] = ["subnet-fixture"]
    data["resources"][1]["dependencies"] = ["vpc-fixture", "missing-fixture"]
    plan = assess(changed_inventory(inventory, **data))
    subnet = next(a for a in plan.assessments if a.resource_id == "subnet-fixture")
    assert "DEPENDENCY_CYCLE_OR_DEPENDENT" in subnet.blockers
    assert "UNRESOLVED_DEPENDENCY" in subnet.blockers


def test_checkpoint_survives_restart_and_review_cannot_execute(tmp_path):
    inventory = fixture()
    path = str(tmp_path / "checkpoint.sqlite")
    user = principal(inventory)
    with SqliteSaver.from_conn_string(path) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        result = service.start(user, inventory.scope)
        assert result["__interrupt__"]
        state = service.read(user, inventory.scope)
        plan_digest = digest(AssessmentPlan.model_validate_json(state["plan_json"]))
    with SqliteSaver.from_conn_string(path) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        state = service.review(
            user, inventory.scope, ReviewDecision(plan_digest=plan_digest, acknowledged=True)
        )
        assert state["status"] == "REVIEWED_BLOCKED"
        assert state["reviewer"] == user.subject
        assert not AssessmentPlan.model_validate_json(state["plan_json"]).execution_enabled
        with pytest.raises(AccessDenied, match="awaiting"):
            service.review(
                user, inventory.scope, ReviewDecision(plan_digest=plan_digest, acknowledged=True)
            )


def test_invalid_review_does_not_consume_pending_review(tmp_path):
    inventory = fixture()
    user = principal(inventory)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        service.start(user, inventory.scope)
        with pytest.raises(AccessDenied, match="Stale"):
            service.review(
                user, inventory.scope, ReviewDecision(plan_digest="0" * 64, acknowledged=True)
            )
        assert service.read(user, inventory.scope)["status"] == "AWAITING_REVIEW"
        state = service.read(user, inventory.scope)
        result = service.review(
            user,
            inventory.scope,
            ReviewDecision(
                plan_digest=digest(AssessmentPlan.model_validate_json(state["plan_json"])),
                acknowledged=False,
            ),
        )
        assert result["status"] == "REVIEW_REJECTED"


def test_checkpoint_read_and_resume_require_authorized_context(tmp_path):
    inventory = fixture()
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        service.start(principal(inventory), inventory.scope)
        attacker = Principal(tenant_id=uuid4(), subject="attacker", roles=("reviewer",))
        with pytest.raises(AccessDenied):
            service.read(attacker, inventory.scope)
        with pytest.raises(AccessDenied):
            service.review(
                attacker, inventory.scope, ReviewDecision(plan_digest="0" * 64, acknowledged=True)
            )
        with pytest.raises(AccessDenied):
            service.review(
                principal(inventory, ("assessor",)),
                inventory.scope,
                ReviewDecision(plan_digest="0" * 64, acknowledged=True),
            )
        with pytest.raises(AccessDenied, match="exists"):
            service.start(principal(inventory), inventory.scope)


def test_modified_scope_cannot_reuse_checkpoint(tmp_path):
    inventory = fixture()
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        service.start(principal(inventory), inventory.scope)
        data = inventory.scope.model_dump()
        data["regions"] = ("eu-west-1",)
        with pytest.raises(AccessDenied, match="scope mismatch"):
            service.read(principal(inventory), Scope(**data))


def test_model_cannot_enable_execution_or_coerce_approval():
    data = assess(fixture()).model_dump(mode="json")
    data["execution_enabled"] = True

    with pytest.raises(ValidationError):
        AssessmentPlan.model_validate_json(json.dumps(data))
    with pytest.raises(ValidationError):
        ReviewDecision(plan_digest="0" * 64, acknowledged="true")


def test_untrusted_arguments_cannot_grant_tool_permission():
    with pytest.raises(ValidationError):
        ToolRequest(
            name="read_inventory",
            scope=fixture().scope,
            request_id="attempt",
            execution_enabled=True,
        )


def test_changed_inventory_cannot_reuse_receipt():
    from infra_migration.workflow import assess_node

    original = fixture()
    data = original.model_dump(mode="json")
    data["resources"][0]["owner"] = "unknown"
    altered = changed_inventory(original, **data)
    with pytest.raises(AccessDenied, match="receipt mismatch"):
        assess_node(
            {"inventory_json": altered.model_dump_json(), "receipt_digest": digest(original)}
        )
