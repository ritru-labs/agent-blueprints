"""Read-only Phase 1F observation entrypoint for an existing durable draft PR."""

import argparse
import json
import os
import pathlib
import sys

from database import Store
from github_api import GitHubAPI
from phase1f_observer import ObservationPolicy, TrustedPRObserver
from policy import Policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--run-policy", type=pathlib.Path, required=True)
    parser.add_argument("--observation-policy", type=pathlib.Path, required=True)
    parser.add_argument("--interrupt-after-read", action="store_true")
    args = parser.parse_args()
    database = Store(os.environ.get("PHASE1C_DATABASE_URL"))
    database.migrate()
    run_policy = Policy.load(args.run_policy)
    observation_policy = ObservationPolicy.from_document(
        json.loads(args.observation_policy.read_text()))
    batch = TrustedPRObserver(database, run_policy, observation_policy, GitHubAPI()).observe(
        args.task, interrupt_after_read=args.interrupt_after_read)
    print(json.dumps({"observation_id": str(batch["id"]), "run_id": str(batch["run_id"]),
                      "head_commit_sha": batch["head_commit_sha"],
                      "gate": batch["gate"], "findings": batch["findings"]}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"Phase 1F observation stopped: {error}", file=sys.stderr)
        sys.exit(1)
