"""Run a synthetic assessment with a local durable SQLite checkpoint."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from langgraph.checkpoint.sqlite import SqliteSaver

from .models import AssessmentPlan, Inventory, Principal, Resource, ReviewDecision, Scope, digest
from .tools import FixtureGateway
from .workflow import AssessmentService


def fixture(workflow="manual_adoption"):
    scope = Scope(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        account_id="000000000000",
        regions=("us-east-1",),
        workflow=workflow,
    )
    owner = "manual" if workflow == "manual_adoption" else "cloudformation"
    return Inventory(
        scope=scope,
        provenance="synthetic_fixture",
        observed_at=datetime(2026, 10, 4, tzinfo=UTC),
        coverage="partial",
        gaps=("Synthetic inventory; no cloud API was contacted",),
        resources=(
            Resource(
                resource_id="vpc-fixture",
                resource_type="AWS::EC2::VPC",
                account_id=scope.account_id,
                region="us-east-1",
                owner=owner,
            ),
            Resource(
                resource_id="subnet-fixture",
                resource_type="AWS::EC2::Subnet",
                account_id=scope.account_id,
                region="us-east-1",
                owner=owner,
                dependencies=("vpc-fixture",),
            ),
            Resource(
                resource_id="unsupported-fixture",
                resource_type="AWS::Custom::Resource",
                account_id=scope.account_id,
                region="us-east-1",
                owner="unknown",
            ),
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=Path("demo.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("demo-report.json"))
    parser.add_argument(
        "--workflow",
        choices=["manual_adoption", "cloudformation_migration"],
        default="manual_adoption",
    )
    parser.add_argument(
        "--acknowledge",
        action="store_true",
        help="Acknowledge the fixture assessment; never executes a migration",
    )
    args = parser.parse_args()
    inventory = fixture(args.workflow)
    principal = Principal(
        tenant_id=inventory.scope.tenant_id,
        subject="fixture-operator",
        roles=("assessor", "reviewer"),
    )
    with SqliteSaver.from_conn_string(str(args.checkpoint)) as saver:
        service = AssessmentService(FixtureGateway(inventory), saver)
        state = service.read(principal, inventory.scope)
        if not state:
            service.start(principal, inventory.scope)
            state = service.read(principal, inventory.scope)
        plan = AssessmentPlan.model_validate_json(state["plan_json"])
        if args.acknowledge and state["status"] == "AWAITING_REVIEW":
            state = service.review(
                principal,
                inventory.scope,
                ReviewDecision(plan_digest=digest(plan), acknowledged=True),
            )
        report = {
            "status": state["status"],
            "provenance": "synthetic_fixture",
            "plan_digest": digest(plan),
            "plan": plan.model_dump(mode="json"),
            "live_cloud_calls": 0,
            "live_model_calls": 0,
            "resource_writes": 0,
            "reviewer": state.get("reviewer"),
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "report": str(args.output),
                    "execution_enabled": False,
                    "provenance": "synthetic_fixture",
                }
            )
        )


if __name__ == "__main__":
    main()
