import json
from copy import deepcopy

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from test_migration import Model, actors, configured_inventory

from infra_migration.assessment import assess
from infra_migration.legacy_pipeline import LegacyMigrationPipeline
from infra_migration.models import ReviewDecision, digest
from infra_migration.pipeline import MigrationPipeline
from infra_migration.reasoning import ModelReviewer
from infra_migration.specialists import (
    ORDER,
    VERSION,
    GenerationSpecialist,
    PlanningRequest,
    PlanningSpecialist,
    RecoveryPlan,
    check_handoffs,
)
from infra_migration.tools import AccessDenied


def prepare(tmp_path, saver, *, reviewer=None, runner=None, discover=None):
    inventory = configured_inventory()
    assessor, _, _ = actors(inventory)
    return inventory, MigrationPipeline(
        assessor,
        inventory.scope,
        discover or (lambda: inventory),
        saver,
        model_reviewer=reviewer,
        runner=runner,
        output_directory=tmp_path / "bundle",
    )


def test_specialist_subgraphs_emit_typed_bound_handoffs_and_review_plans(tmp_path):
    with SqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
        inventory, pipeline = prepare(tmp_path, saver, reviewer=ModelReviewer(Model()))
        result = pipeline.start(("vpc-fixture", "subnet-fixture"))
        assert result["architecture_version"] == VERSION
        handoffs = check_handoffs(result, inventory.scope, complete=True)
        assert tuple(r.specialist for r in handoffs) == ORDER
        assert saver.conn.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints").fetchall() == [
            ("",)
        ]
        assert not any(r.execution_enabled for r in handoffs)
        plan = json.loads(result["migration_plan_json"])
        assert plan["dependency_waves"] == [["vpc-fixture"], ["subnet-fixture"]]
        recovery = RecoveryPlan.model_validate_json(result["recovery_plan_json"])
        assert not recovery.qualified and not recovery.execution_enabled
        assert all(a.status == "requires_live_qualification" for a in recovery.actions)
        assert len(recovery.actions) == 2
        assert (
            recovery.actions[0].required_observations != recovery.actions[1].required_observations
        )
        assert recovery.plan_digest == digest(plan)
        package = result["__interrupt__"][0].value["package"]
        assert package["handoffs_json"] == result["handoffs_json"]
        assert package["recovery_plan_json"] == result["recovery_plan_json"]


@pytest.mark.parametrize(
    "mutation", ["tenant", "selection", "payload", "order", "grant", "truncate"]
)
def test_handoff_tampering_is_blocked_before_review(tmp_path, mutation):
    with SqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
        inventory, pipeline = prepare(tmp_path, saver)
        result = pipeline.start(("vpc-fixture", "subnet-fixture"))
        corrupt = deepcopy(result)
        receipts = json.loads(corrupt["handoffs_json"])
        if mutation == "tenant":
            receipts[0]["scope_digest"] = "a" * 64
        elif mutation == "selection":
            corrupt["selected_ids"] = ("vpc-fixture",)
        elif mutation == "payload":
            corrupt["compile_receipt"] = {"passed": True}
        elif mutation == "order":
            receipts.reverse()
        elif mutation == "grant":
            receipts[0]["execution_enabled"] = True
        else:
            receipts.pop()
        corrupt["handoffs_json"] = json.dumps(receipts)
        with pytest.raises(AccessDenied):
            check_handoffs(corrupt, inventory.scope, complete=True)


def test_resume_does_not_repeat_completed_specialists_or_model_call(tmp_path):
    calls = []
    model = ModelReviewer(Model())
    path = str(tmp_path / "state.sqlite")

    def discover():
        calls.append("read")
        return configured_inventory()

    with SqliteSaver.from_conn_string(path) as saver:
        inventory, pipeline = prepare(tmp_path, saver, reviewer=model, discover=discover)
        result = pipeline.start(("vpc-fixture", "subnet-fixture"))
        decision = result["__interrupt__"][0].value["plan_digest"]
    with SqliteSaver.from_conn_string(path) as saver:
        _, pipeline = prepare(tmp_path, saver, reviewer=model, discover=discover)
        _, reviewer, _ = actors(inventory)
        resumed = pipeline.resume_review(
            reviewer, ReviewDecision(plan_digest=decision, acknowledged=True)
        )
        assert resumed["status"] == "REVIEWED_EXECUTION_BLOCKED"
        assert calls == ["read"] and model.calls == 1
        assert len(check_handoffs(resumed, inventory.scope, complete=True)) == 7


def test_legacy_awaiting_review_retains_original_digest_and_graph(tmp_path):
    inventory = configured_inventory()
    assessor, reviewer, _ = actors(inventory)
    path = str(tmp_path / "legacy.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        old = LegacyMigrationPipeline(
            assessor,
            inventory.scope,
            lambda: inventory,
            saver,
            output_directory=tmp_path / "bundle",
        )
        result = old.start(("vpc-fixture", "subnet-fixture"))
        original = result["__interrupt__"][0].value["plan_digest"]
    with SqliteSaver.from_conn_string(path) as saver:
        pipeline = MigrationPipeline(
            assessor,
            inventory.scope,
            lambda: inventory,
            saver,
            output_directory=tmp_path / "bundle",
        )
        assert pipeline._legacy is not None
        resumed = pipeline.resume_review(
            reviewer, ReviewDecision(plan_digest=original, acknowledged=True)
        )
        assert resumed["package_digest"] == original
        assert resumed["status"] == "REVIEWED_EXECUTION_BLOCKED"
        assert "architecture_version" not in resumed


def test_failed_validation_resumes_without_repeating_upstream_specialists(tmp_path):
    class Runner:
        calls = 0

        def compile(self, bundle, directory):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("disposable compilation interrupted")
            return {"passed": True, "artifact_digest": bundle.artifact_digest}

    runner, calls = Runner(), []

    def discover():
        calls.append("read")
        return configured_inventory()

    with SqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
        inventory, pipeline = prepare(tmp_path, saver, runner=runner, discover=discover)
        with pytest.raises(RuntimeError):
            pipeline.start(("vpc-fixture", "subnet-fixture"))
        assert pipeline._snapshot().next == ("validate",)
        result = pipeline.graph.invoke(None, pipeline.config)
        assert calls == ["read"] and runner.calls == 2
        assert len(check_handoffs(result, inventory.scope, complete=True)) == 7


def test_planner_blocks_missing_dependencies_and_foreign_assessment():
    inventory = configured_inventory()
    planner = PlanningSpecialist()
    with pytest.raises(AccessDenied, match="dependencies"):
        planner.run(
            PlanningRequest(
                inventory=inventory, assessment=assess(inventory), selected_ids=("subnet-fixture",)
            )
        )
    forged = assess(inventory).model_copy(update={"inventory_digest": "a" * 64})
    with pytest.raises(AccessDenied, match="bind"):
        planner.run(
            PlanningRequest(inventory=inventory, assessment=forged, selected_ids=("vpc-fixture",))
        )


def test_specialist_cannot_receive_raw_supervisor_state_or_authorize_execution():
    agent = GenerationSpecialist()
    with pytest.raises(AccessDenied, match="contract"):
        agent.run({"execution_enabled": True, "credential": "not-an-agent-input"})


def test_model_block_stops_before_generation_or_validation(tmp_path):
    from infra_migration.reasoning import ModelProposal, Recommendation

    class Reviewer:
        last_receipt = {"model": "fixture"}

        def review(self, inventory, documents):
            return ModelProposal(
                summary="Block selected VPC",
                recommendations=[
                    Recommendation(
                        resource_alias="resource-0",
                        disposition="blocked",
                        rationale="Requires review",
                    ),
                ],
            )

    with SqliteSaver.from_conn_string(str(tmp_path / "blocked.sqlite")) as saver:
        _, pipeline = prepare(tmp_path, saver, reviewer=Reviewer())
        with pytest.raises(AccessDenied, match="blocked"):
            pipeline.start(("vpc-fixture", "subnet-fixture"))
        assert not (tmp_path / "bundle").exists()
        assert pipeline._snapshot().next == ("plan",)


def test_unknown_checkpoint_version_does_not_fall_back_to_legacy(tmp_path):
    path = str(tmp_path / "version.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        _, pipeline = prepare(tmp_path, saver)
        pipeline.start(("vpc-fixture", "subnet-fixture"))
        pipeline.graph.update_state(pipeline.config, {"architecture_version": "unknown-v99"})
        with pytest.raises(AccessDenied, match="version"):
            prepare(tmp_path, saver)
