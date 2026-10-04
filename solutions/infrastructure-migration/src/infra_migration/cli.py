"""Operator CLI. Cloud reads are explicit; no command enables live execution."""

import argparse
import getpass
from pathlib import Path

import boto3
from langgraph.checkpoint.sqlite import SqliteSaver

from .discovery import AwsDiscovery
from .generation import ProjectBundle, inventory_fingerprint, verify_bundle_directory
from .ledger import LocalLedger
from .models import Inventory, Principal, ReviewDecision, Scope
from .pipeline import MigrationPipeline
from .reasoning import ModelReviewer, fetch_document
from .runner import DockerRunner
from .source import parse_template, release_proposal, retention_proposal
from .tools import AccessDenied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser(
        "discover", help="Read VPC/subnet configuration from an approved AWS profile"
    )
    discover.add_argument("--scope", type=Path, required=True)
    discover.add_argument("--profile", required=True)
    discover.add_argument("--output", type=Path, required=True)
    discover.add_argument("--max-calls", type=int, default=200)
    prepare = commands.add_parser(
        "prepare", help="Prepare a review package from an inventory snapshot"
    )
    prepare.add_argument("--inventory", type=Path, required=True)
    prepare.add_argument("--resource", action="append", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--checkpoint", type=Path, required=True)
    prepare.add_argument("--runner-image")
    prepare.add_argument("--model")
    prepare.add_argument("--model-base-url")
    prepare.add_argument("--official-docs", action="store_true")
    prepare.add_argument("--acknowledge-digest")
    verify = commands.add_parser(
        "compile", help="Compile an exact generated bundle in an isolated Docker runner"
    )
    verify.add_argument("--project", type=Path, required=True)
    verify.add_argument("--runner-image", required=True)
    source = commands.add_parser(
        "source-proposal", help="Generate retention or release proposals without deploying"
    )
    source.add_argument("--template", type=Path, required=True)
    source.add_argument("--logical-id", action="append", required=True)
    source.add_argument("--action", choices=["retain", "release"], required=True)
    source.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import json

    if args.command == "discover":
        scope = Scope.model_validate_json(args.scope.read_text())
        principal = Principal(
            tenant_id=scope.tenant_id, subject=getpass.getuser(), roles=("assessor",)
        )
        inventory = AwsDiscovery(
            boto3.Session(profile_name=args.profile), max_calls=args.max_calls
        ).collect(principal, scope)
        args.output.write_text(inventory.model_dump_json(indent=2) + "\n")
        print(
            json.dumps(
                {
                    "provenance": "aws_api",
                    "resources": len(inventory.resources),
                    "coverage": inventory.coverage,
                    "gaps": inventory.gaps,
                }
            )
        )
    elif args.command == "prepare":
        inventory = Inventory.model_validate_json(args.inventory.read_text())
        principal = Principal(
            tenant_id=inventory.scope.tenant_id,
            subject=getpass.getuser(),
            roles=("assessor", "reviewer"),
        )
        docs = (
            tuple(fetch_document(key) for key in ("vpc", "subnet", "adoption"))
            if args.official_docs
            else ()
        )
        model = (
            ModelReviewer.openai_compatible(args.model, args.model_base_url) if args.model else None
        )
        if model is not None:
            ledger = LocalLedger(args.checkpoint.with_suffix(".budget.sqlite"))
            model.reserve_call = lambda: ledger.reserve_model_call(principal, inventory.scope)
        runner = DockerRunner(args.runner_image) if args.runner_image else None
        with SqliteSaver.from_conn_string(str(args.checkpoint)) as saver:
            pipeline = MigrationPipeline(
                principal,
                inventory.scope,
                lambda: inventory,
                saver,
                model_reviewer=model,
                documents=docs,
                runner=runner,
                output_directory=args.output,
            )
            state = pipeline._snapshot()
            if not state.values:
                result = pipeline.start(tuple(args.resource))
                state = pipeline._snapshot()
            else:
                result = state.values
                saved_inventory = Inventory.model_validate_json(result["inventory_json"])
                if inventory_fingerprint(saved_inventory) != inventory_fingerprint(
                    inventory
                ) or tuple(result["selected_ids"]) != tuple(args.resource):
                    raise AccessDenied(
                        "Checkpoint does not match the requested inventory and resources"
                    )
                verify_bundle_directory(
                    ProjectBundle.model_validate_json(result["bundle_json"]), args.output
                )
            if args.acknowledge_digest:
                result = pipeline.resume_review(
                    principal,
                    ReviewDecision(plan_digest=args.acknowledge_digest, acknowledged=True),
                )
            pending = pipeline._snapshot().tasks
            interrupts = [i.value for task in pending for i in task.interrupts]
            print(
                json.dumps(
                    {
                        "status": result.get("status", "AWAITING_PACKAGE_REVIEW"),
                        "pending_review_digests": [i["plan_digest"] for i in interrupts],
                        "execution_enabled": False,
                        "input_provenance": "operator_supplied_snapshot",
                    }
                )
            )
    elif args.command == "compile":
        bundle = ProjectBundle.model_validate_json((args.project / "bundle.json").read_text())
        print(json.dumps(DockerRunner(args.runner_image).compile(bundle, args.project)))
    else:
        template = parse_template(args.template.read_text())
        proposal = (
            retention_proposal(template, tuple(args.logical_id))
            if args.action == "retain"
            else release_proposal(template, tuple(args.logical_id))
        )
        args.output.write_text(json.dumps(proposal, indent=2) + "\n")
        print(json.dumps({"proposal": str(args.output), "cloud_writes": 0}))


if __name__ == "__main__":
    main()
