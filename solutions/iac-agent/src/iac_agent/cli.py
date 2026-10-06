"""iac-agent CLI. One run = one account + one region, resumable across commands.

  scan     guard, discover, classify; stops for scope sign-off (writes scope_candidates.json)
  plan     resume with the signed-off scope; generate, check, repair, plan; stops for approval
  approve  resume with approval: re-scan, import, verify, report
  reject   resume with rejection: nothing is imported
  status   show where the run is

The run's checkpoint lives in <workdir>/run.sqlite.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import boto3
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .adapters.aws.adapter import AwsAdapter
from .core.graph import Deps, build, start_state
from .core.repair import BedrockRepairer
from .core.scanners import Scanners
from .core.terraform import Terraform


def _no_llm(block, problems):  # repair attempts fail and the resource is skipped
    return ""


def _graph(args, conn):
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    repairer = (
        BedrockRepairer(session.client("bedrock-runtime", region_name=args.region), args.model_id)
        if args.model_id
        else _no_llm
    )
    deps = Deps(
        adapter=AwsAdapter(session, args.region, state_files=args.state_file or ()),
        terraform=Terraform(args.workdir),
        scanners=Scanners(),
        repairer=repairer,
        workdir=args.workdir,
        backend_hcl=Path(args.backend).read_text() if args.backend else None,
    )
    return build(deps, SqliteSaver(conn))


def _pending_step(graph, config) -> str | None:
    pending = [i.value for t in graph.get_state(config).tasks for i in t.interrupts]
    return pending[0]["step"] if pending else None


def _show(graph, config, workdir: Path) -> int:
    snapshot = graph.get_state(config)
    pending = [i.value for t in snapshot.tasks for i in t.interrupts]
    values = snapshot.values
    if pending:
        step = pending[0]["step"]
        (workdir / f"{step}.json").write_text(json.dumps(pending[0], indent=2))
        print(f"Waiting for {step}: see {workdir / (step + '.json')}")
        return 0
    print(f"Run {values.get('run_id')}: {values.get('status')}. Report: {workdir / 'ADOPTION_REPORT.md'}")
    return 0 if values.get("status") in ("adopted", "nothing_to_adopt") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iac-agent")
    p.add_argument("command", choices=["scan", "plan", "approve", "reject", "status"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--account", help="signed-off account ID (scan)")
    p.add_argument("--region", required=True)
    p.add_argument("--profile")
    p.add_argument("--model-id", help="Bedrock model ID for repair; without it, nothing is sent to an LLM")
    p.add_argument("--state-file", type=Path, action="append", help="client Terraform state (repeatable)")
    p.add_argument("--backend", help="file with the backend block to use")
    p.add_argument("--scope", type=Path, help="plan: JSON list of [type, import_id] to adopt, or omit for all")
    args = p.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(args.workdir / "run.sqlite", check_same_thread=False) as conn:
        graph = _graph(args, conn)
        config = {"configurable": {"thread_id": args.run_id}, "recursion_limit": 500}
        if args.command == "scan":
            if not args.account:
                p.error("scan needs --account")
            graph.invoke(start_state(args.run_id, args.account, args.region), config)
        elif (
            expected := {"plan": "scope_signoff", "approve": "approve_plan", "reject": "approve_plan"}.get(args.command)
        ) and _pending_step(graph, config) != expected:
            p.error(f"{args.command} needs the run to be waiting for {expected}")
        elif args.command == "plan":
            scope = json.loads(args.scope.read_text()) if args.scope else "all"
            graph.invoke(Command(resume=scope), config)
        elif args.command in ("approve", "reject"):
            graph.invoke(Command(resume=args.command == "approve"), config)
        return _show(graph, config, args.workdir)


if __name__ == "__main__":
    sys.exit(main())
