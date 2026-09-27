"""Separate process for observation crash/restart integration tests."""

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import Store
from fake_github import FakeGitHub
from phase1f_observer import ObservationPolicy, TrustedPRObserver
from policy import Policy


def main():
    task, run_policy, observation_policy, fake_path, interrupt = sys.argv[1:]
    database = Store(os.environ["PHASE1C_DATABASE_URL"])
    database.migrate()
    observer = TrustedPRObserver(
        database, Policy.load(run_policy),
        ObservationPolicy.from_document(json.loads(pathlib.Path(observation_policy).read_text())),
        FakeGitHub(fake_path),
    )
    row = observer.observe(task, interrupt_after_read=interrupt == "yes")
    print(json.dumps({"id": str(row["id"]), "gate": row["gate"]}))


if __name__ == "__main__":
    main()
