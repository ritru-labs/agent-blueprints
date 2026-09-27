"""Explicit GitHub publication entrypoint; no writes with a v1 local policy."""

import argparse
import json
import os
import pathlib
import sys

from adapters import ContentAddressedStore
from database import Store
from draft_pr import DraftPRCoordinator
from github_api import GitHubAPI
from github_publisher import GitHubPublisher
from policy import Policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("publish", "draft-pr"))
    parser.add_argument("--task", required=True)
    parser.add_argument("--store-dir", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--interrupt-after-write", action="store_true")
    args = parser.parse_args()
    policy = Policy.load(args.policy)
    if policy.document["schema_version"] != 2:
        raise ValueError("GitHub action requires explicit version 2 write policy")
    database = Store(os.environ.get("PHASE1C_DATABASE_URL"))
    database.migrate()
    artifact_store = ContentAddressedStore(args.store_dir)
    github = GitHubAPI()
    if args.action == "publish":
        result = GitHubPublisher(database, policy, artifact_store, github).publish(
            args.task, interrupt_after_push=args.interrupt_after_write)
    else:
        result = DraftPRCoordinator(database, policy, artifact_store, github).create_or_reconcile(
            args.task, interrupt_after_create=args.interrupt_after_write)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"GitHub action stopped: {error}", file=sys.stderr)
        sys.exit(1)
