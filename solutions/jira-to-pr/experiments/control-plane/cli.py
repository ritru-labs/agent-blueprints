"""Synthetic-only Phase 1C CLI. Never performs Jira or GitHub writes."""

import argparse
import json
import os
import pathlib
import sys

from adapters import ContentAddressedStore, Phase1ASavedArtifact, Phase1BTrustedVerifier
from controller import Controller
from database import Store
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parent
INTERRUPT_EXIT = 75


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("PHASE1C_DATABASE_URL"))
    parser.add_argument("--store-dir", type=pathlib.Path,
                        default=HERE / ".control-runs/artifacts")
    parser.add_argument("--policy", type=pathlib.Path, default=HERE / "policy.json")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db")
    start = subcommands.add_parser("start")
    start.add_argument("--task", required=True)
    start.add_argument("--source-archive", type=pathlib.Path)
    start.add_argument("--interrupt-after-candidate", action="store_true")
    resume = subcommands.add_parser("resume")
    resume.add_argument("--task", required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--task", required=True)
    args = parser.parse_args()

    database = Store(args.dsn)
    if args.command == "init-db":
        database.migrate()
        print("Phase 1C/1D PostgreSQL schema version 2 ready")
        return 0
    policy = Policy.load(args.policy)
    controller = Controller(database, policy, ContentAddressedStore(args.store_dir),
                            Phase1BTrustedVerifier())
    if args.command == "start":
        state = controller.admit_candidate(args.task, Phase1ASavedArtifact(args.source_archive))
        print(json.dumps(state, sort_keys=True), flush=True)
        if args.interrupt_after_candidate:
            print("Intentional process exit after durable candidate commit", flush=True)
            os._exit(INTERRUPT_EXIT)
        state = controller.resume(args.task)
    elif args.command == "resume":
        state = controller.resume(args.task)
    else:
        state = database.summary(args.task)
    print(json.dumps(state, sort_keys=True))
    return 0 if state["state"] in ("CANDIDATE_READY", "VERIFIED") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"Phase 1C error: {error}", file=sys.stderr)
        raise SystemExit(1)
