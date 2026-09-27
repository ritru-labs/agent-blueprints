"""Trusted local bridge for the Phase 1D-B Agents API adapter; no external writes."""

import argparse
import json
import os
import pathlib
import sys

from adapters import ContentAddressedStore, Phase1BTrustedVerifier, SavedTurnArtifact
from controller import Controller
from database import Store
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parent


def safe_json(value):
    return {key: str(item) if key in ("id",) else item for key, item in value.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("PHASE1C_DATABASE_URL"))
    parser.add_argument("--store-dir", type=pathlib.Path,
                        default=HERE / ".control-runs/artifacts")
    parser.add_argument("--policy", type=pathlib.Path, default=HERE / "policy.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    register = commands.add_parser("register-session")
    register.add_argument("--task", required=True)
    register.add_argument("--session-id", required=True)
    admit = commands.add_parser("admit")
    for flag in ("task", "session-id", "turn-id", "artifact-id", "archive-sha256", "archive"):
        admit.add_argument(f"--{flag}", required=True)
    for name in ("verify", "plan", "mark-uncertain", "inspect", "history",
                 "ci-plan", "ci-mark-uncertain", "ci-status", "ci-observation-status"):
        command = commands.add_parser(name)
        command.add_argument("--task", required=True)
    observe = commands.add_parser("observe")
    for flag in ("task", "session-id", "input-sha256", "message-item-id", "turn-id"):
        observe.add_argument(f"--{flag}", required=True)
    ci_observe = commands.add_parser("ci-observe")
    for flag in ("task", "session-id", "input-sha256", "message-item-id", "turn-id"):
        ci_observe.add_argument(f"--{flag}", required=True)
    replace = commands.add_parser("replace-session")
    for flag in ("task", "predecessor-id", "replacement-id"):
        replace.add_argument(f"--{flag}", required=True)
    args = parser.parse_args()

    database = Store(args.dsn)
    if args.command == "init-db":
        database.migrate()
        print(json.dumps({"schema_version": 2, "status": "ready"}))
        return
    policy = Policy.load(args.policy)
    controller = Controller(database, policy, ContentAddressedStore(args.store_dir),
                            Phase1BTrustedVerifier())
    if args.command == "register-session":
        database.create_run(args.task, args.session_id, policy)
        value = database.summary(args.task)
    elif args.command == "admit":
        value = controller.admit_candidate(args.task, SavedTurnArtifact(
            args.session_id, args.turn_id, args.artifact_id, args.archive_sha256,
            args.archive))
    elif args.command == "verify":
        value = controller.resume(args.task)
    elif args.command == "plan":
        value = controller.plan_repair(args.task)
    elif args.command == "mark-uncertain":
        value = safe_json(controller.mark_repair_uncertain(args.task))
    elif args.command == "ci-plan":
        value = controller.plan_ci_repair(args.task)
    elif args.command == "ci-mark-uncertain":
        value = safe_json(controller.mark_ci_repair_uncertain(args.task))
    elif args.command == "ci-status":
        run = database.get_run(args.task)
        attempt = database.current_ci_repair(run["id"])
        value = safe_json(attempt) if attempt else {"status": "ABSENT"}
    elif args.command == "ci-observation-status":
        run = database.get_run(args.task)
        observation = database.latest_pr_observation(run["id"])
        value = ({"status": "ABSENT"} if observation is None else
                 {"status": "OBSERVED", "gate": observation["gate"],
                  "candidate_id": str(observation["candidate_id"]),
                  "head_commit_sha": observation["head_commit_sha"]})
    elif args.command == "observe":
        value = safe_json(controller.observe_repair(
            args.task, args.session_id, args.input_sha256,
            args.message_item_id, args.turn_id))
    elif args.command == "ci-observe":
        value = safe_json(controller.observe_ci_repair(
            args.task, args.session_id, args.input_sha256,
            args.message_item_id, args.turn_id))
    elif args.command == "replace-session":
        run = database.get_run(args.task)
        database.assert_policy(run, policy)
        with database.worker_lock(run["id"]):
            database.replace_session(run["id"], args.predecessor_id, args.replacement_id)
        value = database.summary(args.task)
    elif args.command == "history":
        value = database.history(args.task)
    else:
        value = database.summary(args.task)
    print(json.dumps(value, sort_keys=True, default=str))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"control-plane bridge error: {error}", file=sys.stderr)
        raise SystemExit(1)
