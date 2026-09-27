"""Local Phase 1E publisher. Requires an explicitly supplied bare Git remote."""

import argparse
import json
import os
import pathlib
import sys

from adapters import ContentAddressedStore
from database import Store
from policy import Policy
from trusted_publisher import LocalBarePublisher

HERE = pathlib.Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--store-dir", type=pathlib.Path, required=True)
    parser.add_argument("--local-bare-remote", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, default=HERE / "policy.json")
    parser.add_argument("--interrupt-after-push", action="store_true")
    args = parser.parse_args()
    database = Store(os.environ.get("PHASE1C_DATABASE_URL"))
    database.migrate()
    publisher = LocalBarePublisher(database, Policy.load(args.policy),
                                   ContentAddressedStore(args.store_dir),
                                   args.local_bare_remote)
    result = publisher.publish(args.task, interrupt_after_push=args.interrupt_after_push)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["publication_state"] == "CONFIRMED" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"publication stopped: {error}", file=sys.stderr)
        sys.exit(1)
