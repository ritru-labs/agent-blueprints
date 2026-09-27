"""Separate process for a branch update that intentionally dies after Git push."""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import ContentAddressedStore
from database import Store
from fake_git_transport import FakeGitTransport
from fake_github import FakeGitHub
from policy import Policy


def main():
    task, policy_path, store_path, remote, fake_path, interrupt = sys.argv[1:]
    database = Store(os.environ["PHASE1C_DATABASE_URL"])
    database.migrate()
    publisher = FakeGitTransport(database, Policy.load(policy_path),
                                 ContentAddressedStore(store_path), remote,
                                 FakeGitHub(fake_path))
    publisher.publish(task, interrupt_after_push=interrupt == "yes")


if __name__ == "__main__":
    main()
