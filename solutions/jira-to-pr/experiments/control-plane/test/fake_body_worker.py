"""Separate process for uncertain same-PR body update recovery."""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import ContentAddressedStore
from database import Store
from fake_github import FakeGitHub
from policy import Policy
from pr_body_update import DraftPRBodyCoordinator


def main():
    task, policy_path, store_path, fake_path, interrupt = sys.argv[1:]
    database = Store(os.environ["PHASE1C_DATABASE_URL"])
    database.migrate()
    coordinator = DraftPRBodyCoordinator(database, Policy.load(policy_path),
                                         ContentAddressedStore(store_path),
                                         FakeGitHub(fake_path))
    coordinator.sync(task, interrupt_after_update=interrupt == "yes")


if __name__ == "__main__":
    main()
