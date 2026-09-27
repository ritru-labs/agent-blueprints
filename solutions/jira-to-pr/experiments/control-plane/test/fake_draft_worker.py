"""Separate process used by the draft-PR uncertain-write recovery test."""

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import ContentAddressedStore  # noqa: E402
from database import Store  # noqa: E402
from draft_pr import DraftPRCoordinator  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
from policy import Policy  # noqa: E402


def main():
    task, store_dir, policy_path, fake_state, interrupt = sys.argv[1:]
    database = Store(os.environ["PHASE1C_DATABASE_URL"])
    database.migrate()
    result = DraftPRCoordinator(
        database, Policy.load(policy_path), ContentAddressedStore(store_dir),
        FakeGitHub(fake_state),
    ).create_or_reconcile(task, interrupt_after_create=(interrupt == "yes"))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
