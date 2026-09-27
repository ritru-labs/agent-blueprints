"""Durable draft-PR intent and saved GitHub-state reconciliation tests."""

import getpass
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile

import psycopg
from psycopg import sql

from adapters import ContentAddressedStore, SavedTurnArtifact
from controller import Controller
from database import Store
from draft_pr import DraftPRConflict, DraftPRCoordinator
from github_api import REPOSITORY
from github_publisher import GitHubPublisher
from policy import Policy
from test_publication import AcceptingVerifier, SPIKE
from fake_github import FakeGitHub

HERE = pathlib.Path(__file__).resolve().parents[1]
BASE_REF = "refs/heads/qualification-base"
COMMIT = "a" * 40
GIT_TREE = "b" * 40


class DraftPRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("initdb", "pg_ctl"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="phase1e-draft-pg-", dir="/private/tmp")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        cls.data = cls.root / "db"
        initial = subprocess.run(["initdb", "-D", str(cls.data), "-A", "trust",
                                  "--no-instructions"], capture_output=True, text=True, timeout=40)
        if initial.returncode:
            raise RuntimeError(f"initdb failed: {initial.stderr[-500:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={cls.socket} -c unix_socket_permissions=0700"
        started = subprocess.run(["pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                                  "-o", options, "-w", "start"], capture_output=True,
                                 text=True, timeout=40)
        if started.returncode:
            raise RuntimeError(f"pg_ctl failed: {started.stderr[-500:]}")
        cls.admin_dsn = f"host={cls.socket} dbname=postgres user={getpass.getuser()}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "data") and cls.data.exists():
            subprocess.run(["pg_ctl", "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
                           capture_output=True, timeout=40)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def setUp(self):
        tag = uuid.uuid4().hex[:12]
        name = f"draft_{tag}"
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.dsn = f"host={self.socket} dbname={name} user={getpass.getuser()}"
        self.database = Store(self.dsn)
        self.database.migrate()
        document = json.loads((HERE / "policy.json").read_text())
        document.update({"schema_version": 2, "external_writes_enabled": True,
                         "allowed_operation_kinds": ["git_branch", "draft_pr"],
                         "github_target_base": BASE_REF,
                         "github_actor_login": "binnukyadari"})
        self.policy_path = self.root / f"policy-{tag}.json"
        self.policy_path.write_text(json.dumps(document))
        self.policy = Policy.load(self.policy_path)
        self.store = ContentAddressedStore(self.root / f"artifacts-{tag}")
        self.task = f"DRAFT-{tag}"
        archive = self.root / f"source-{tag}.zip"
        app = (SPIKE / "sample/app.py").read_text() + "\n\ndef add(a, b):\n    return a + b\n"
        tests = ((SPIKE / "sample/tests/test_app.py").read_text() +
                 "\nfrom sample.app import add\n\nclass AdditionTests(unittest.TestCase):\n"
                 "    def test_add(self):\n        self.assertEqual(add(2, -3), -1)\n")
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sample/__init__.py", (SPIKE / "sample/__init__.py").read_bytes())
            bundle.writestr("sample/app.py", app)
            bundle.writestr("sample/tests/test_app.py", tests)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        source = SavedTurnArtifact(f"sess_{tag}", f"turn_{tag}", f"artifact_{tag}", digest, archive)
        controller = Controller(self.database, self.policy, self.store,
                                AcceptingVerifier(self.policy))
        controller.admit_candidate(self.task, source)
        self.assertEqual(controller.resume(self.task)["state"], "VERIFIED")
        self.run = self.database.get_run(self.task)
        self.candidate = self.database.get_candidate(self.run)
        self.verification = self.database.get_verification(self.run)
        self.head_ref = f"refs/heads/agent/{self.run['id']}"
        intent = {
            "candidate_id": self.candidate["id"],
            "verification_id": self.verification["id"],
            "archive_sha256": self.candidate["archive_sha256"],
            "candidate_tree_sha256": self.candidate["tree_sha256"],
            "base_commit": self.run["base_commit"], "git_tree_sha": GIT_TREE,
            "commit_sha": COMMIT, "branch_ref": self.head_ref,
            "remote_id": hashlib.sha256(GitHubPublisher.REMOTE.encode()).hexdigest(),
            "publisher_policy_hash": "c" * 64,
            "operation_key": hashlib.sha256(f"{self.run['id']}:publication".encode()).hexdigest(),
            "remote_kind": "github",
        }
        self.publication = self.database.plan_publication(self.run["id"], intent)
        self.database.mark_publication_unknown(self.run["id"])
        self.database.confirm_publication(self.run["id"])
        self.fake_path = self.root / f"fake-{tag}.json"
        self.fake_path.write_text(json.dumps({
            "actor_login": "binnukyadari",
            "refs": {BASE_REF: self.run["base_commit"], self.head_ref: COMMIT},
            "trees": {COMMIT: GIT_TREE}, "pulls": [],
        }))
        self.fake = FakeGitHub(self.fake_path)
        self.coordinator = DraftPRCoordinator(self.database, self.policy, self.store, self.fake)

    def worker(self, interrupt):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env["PHASE1C_DATABASE_URL"] = self.dsn
        return subprocess.run(
            [sys.executable, str(HERE / "test/fake_draft_worker.py"), self.task,
             str(self.store.root), str(self.policy_path), str(self.fake_path), interrupt],
            cwd=HERE, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_process_death_after_create_reconciles_one_draft(self):
        first = self.worker("yes")
        self.assertEqual(first.returncode, 75, first.stderr)
        self.assertEqual(self.database.summary(self.task)["draft_pr_state"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(self.fake._read()["pulls"]), 1)
        second = self.worker("no")
        self.assertEqual(second.returncode, 0, second.stderr)
        state = json.loads(second.stdout)
        self.assertEqual(state["draft_pr_state"], "CONFIRMED")
        self.assertEqual(state["draft_pr_number"], 1)
        repeated = self.worker("no")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(len(self.fake._read()["pulls"]), 1)
        with self.database.connect() as conn:
            count = conn.execute("SELECT count(*) AS n FROM draft_pr_attempts").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_base_head_tree_and_actor_drift_stop_before_intent(self):
        state = self.fake._read()
        for field, value in (("base", "0" * 40), ("head", "1" * 40),
                             ("tree", "2" * 40), ("actor", "someone-else")):
            modified = json.loads(json.dumps(state))
            if field == "base":
                modified["refs"][BASE_REF] = value
            elif field == "head":
                modified["refs"][self.head_ref] = value
            elif field == "tree":
                modified["trees"][COMMIT] = value
            else:
                modified["actor_login"] = value
            self.fake._write(modified)
            with self.subTest(field=field), self.assertRaises(DraftPRConflict):
                self.coordinator.create_or_reconcile(self.task)
        self.fake._write(state)
        self.assertIsNone(self.database.get_draft_pr(self.run["id"]))
        publisher = GitHubPublisher(self.database, self.policy, self.store, self.fake)
        modified = self.fake._read()
        modified["refs"][BASE_REF] = "0" * 40
        self.fake._write(modified)
        with self.assertRaisesRegex(RuntimeError, "base branch moved"):
            publisher._preflight(self.run)

    def test_uncertain_missing_pr_is_not_resubmitted(self):
        class AcceptedButInvisible(FakeGitHub):
            def create_draft_pull(self, **kwargs):
                return {"number": 1}

        invisible = AcceptedButInvisible(self.fake_path)
        coordinator = DraftPRCoordinator(self.database, self.policy, self.store, invisible)
        with self.assertRaisesRegex(DraftPRConflict, "did not return"):
            coordinator.create_or_reconcile(self.task)
        with self.assertRaisesRegex(DraftPRConflict, "do not retry blindly"):
            coordinator.create_or_reconcile(self.task)
        self.assertEqual(self.database.summary(self.task)["draft_pr_state"], "OUTCOME_UNKNOWN")

    def test_conflicting_saved_pr_is_rejected(self):
        state = self.fake._read()
        state["pulls"] = [{
            "number": 1, "html_url": f"https://github.com/{REPOSITORY}/pull/1",
            "title": "wrong", "body": "wrong", "draft": True, "state": "open",
            "user": {"login": "binnukyadari"},
            "base": {"ref": BASE_REF.removeprefix("refs/heads/"),
                     "repo": {"full_name": REPOSITORY}},
            "head": {"ref": self.head_ref.removeprefix("refs/heads/"),
                     "sha": COMMIT, "repo": {"full_name": REPOSITORY}},
        }]
        self.fake._write(state)
        with self.assertRaisesRegex(DraftPRConflict, "differs"):
            self.coordinator.create_or_reconcile(self.task)
        self.assertEqual(self.database.summary(self.task)["draft_pr_state"], "PLANNED")

    def test_tampered_artifact_invalidates_draft_gate(self):
        path = self.store.path(self.candidate["archive_sha256"])
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(DraftPRConflict, "integrity failed"):
            self.coordinator.create_or_reconcile(self.task)
        self.assertEqual(self.database.summary(self.task)["state"], "NEEDS_HUMAN")
        self.assertIsNone(self.database.get_draft_pr(self.run["id"]))

    def test_db_rejects_forged_publication_and_pr_mutation(self):
        self.coordinator.create_or_reconcile(self.task)
        row = self.database.get_draft_pr(self.run["id"])
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE draft_pr_attempts SET candidate_id = %s WHERE id = %s",
                             (uuid.uuid4(), row["id"]))
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE publication_attempts SET remote_kind = 'local_bare' "
                             "WHERE id = %s", (self.publication["id"],))
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE draft_pr_attempts SET state = 'OUTCOME_UNKNOWN', "
                             "pr_number = NULL, pr_url = NULL, confirmed_at = NULL "
                             "WHERE id = %s", (row["id"],))


if __name__ == "__main__":
    unittest.main()
