"""Command line entry point. Re-running the same command resumes where the last run stopped."""

import argparse
import json
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .discover import SUPPORTED, discover
from .graph import build_graph
from .repair import make_repairer
from .terraform import GENERATED, TerraformError, Workspace, gcloud_access_token

STATE_DB = ".agent-state.sqlite"


def show_review(payload: dict, workspace: Path) -> None:
    print("\nZero-change plan reached. Review before importing into Terraform state:\n")
    for line in payload["resources"]:
        print(f"  import  {line}")
    for line in payload["skipped"]:
        print(f"  skipped {line}")
    if payload["repair_log"]:
        print("\nRepair log:")
        for line in payload["repair_log"]:
            print(f"  {line}")
    print(f"\nConfiguration: {workspace / GENERATED}")
    print(f"Plan sha256:   {payload['plan_sha256']}")


def ask_approval(payload: dict, args) -> dict | None:
    if args.approve:
        return {"approve": True, "plan_sha256": args.approve}
    if args.reject:
        return {"approve": False}
    if not sys.stdin.isatty():
        print(f"\nTo import, re-run with --approve {payload['plan_sha256']} (or --reject).")
        return None
    answer = input("\nType 'import' to import these resources, anything else to reject: ").strip()
    return {"approve": answer == "import", "plan_sha256": payload["plan_sha256"]}


def drive(graph, config, args, parser):
    """Start, resume or re-plan, then handle the review gate. Returns None while waiting for a decision."""
    snapshot = graph.get_state(config)
    if args.replan:
        if not snapshot.values.get("resources"):
            parser.error("--replan needs an earlier run in this workspace")
        graph.update_state(config, {"attempts": 0, "status": "", "message": ""}, as_node="scaffold")
        graph.invoke(None, config)
    elif not snapshot.values:
        graph.invoke({"project": args.project}, config)
    elif snapshot.next and snapshot.next != ("review",):
        graph.invoke(None, config)  # resume after a failure

    snapshot = graph.get_state(config)
    if snapshot.next == ("review",):
        payload = snapshot.tasks[0].interrupts[0].value
        show_review(payload, args.workspace)
        decision = ask_approval(payload, args)
        if decision is None:
            return None
        graph.invoke(Command(resume=decision), config)
        snapshot = graph.get_state(config)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="GCP project ID to bring under Terraform")
    parser.add_argument("--workspace", type=Path, required=True, help="Directory for the Terraform files")
    parser.add_argument(
        "--types", nargs="+", choices=sorted(SUPPORTED), help="Cloud Asset types (default: all)"
    )
    parser.add_argument("--max-attempts", type=int, default=3, help="Repair attempts before asking a human")
    parser.add_argument("--provider-version", default=">= 6.0", help="hashicorp/google version constraint")
    parser.add_argument("--replan", action="store_true", help="Re-plan after editing generated.tf by hand")
    parser.add_argument(
        "--approve", metavar="PLAN_SHA256", help="Approve the reviewed plan non-interactively"
    )
    parser.add_argument("--reject", action="store_true", help="Reject the reviewed plan")
    parser.add_argument(
        "--credentials",
        choices=["gcloud", "adc"],
        default="gcloud",
        help="Terraform reads GCP as the active gcloud login (default, same identity as discovery) "
        "or via application default credentials / GOOGLE_CREDENTIALS (CI, service accounts)",
    )
    parser.add_argument(
        "--state-bucket",
        help="GCS bucket for Terraform state (one object per workspace); default: local state file",
    )
    args = parser.parse_args(argv)

    args.workspace.mkdir(parents=True, exist_ok=True)
    token = gcloud_access_token if args.credentials == "gcloud" else None
    workspace = Workspace(args.workspace, access_token=token)
    config = {"configurable": {"thread_id": args.project}}

    with SqliteSaver.from_conn_string(str(args.workspace / STATE_DB)) as saver:
        graph = build_graph(
            workspace=workspace,
            discover=discover,
            repairer=make_repairer(),
            checkpointer=saver,
            asset_types=args.types,
            provider_version=args.provider_version,
            state_bucket=args.state_bucket,
            max_attempts=args.max_attempts,
        )
        try:
            snapshot = drive(graph, config, args, parser)
        except (TerraformError, RuntimeError) as exc:
            # The checkpoint keeps every finished step; re-running the same command resumes.
            print(
                f"\nerror: {exc}\n\nFix the cause, then re-run the same command to resume.", file=sys.stderr
            )
            return 1
        if snapshot is None:
            return 0  # waiting for a review decision

    values = snapshot.values
    print(json.dumps({k: values.get(k) for k in ("status", "message", "attempts")}, indent=2))
    if values.get("status") == "NEEDS_HUMAN":
        print(json.dumps(values["plan"], indent=2))
    return 0 if values.get("status") in ("DONE", "NOTHING_TO_IMPORT") else 1


if __name__ == "__main__":
    sys.exit(main())
