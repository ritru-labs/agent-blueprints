"""Phase 1E exact-tree publication and crash recovery on real PostgreSQL/Git."""

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
from unittest import mock

import psycopg
from psycopg import sql

from adapters import ContentAddressedStore, REPO_ROOT, SavedTurnArtifact
from archive_intake import read_candidate
from config import ALLOWED_FILES, BASE_PREFIX
from controller import Controller
from database import Store
from policy import Policy
from trusted_publisher import LocalBarePublisher, PublicationError

HERE = pathlib.Path(__file__).resolve().parents[1]
SPIKE = HERE.parent / "agents-api-spike/sample-project"


class AcceptingVerifier:
    def __init__(self, policy):
        self.policy = policy

    def verify(self, archive_path, candidate):
        return {
            "status": "PASS", "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_commit": candidate["base_commit"],
            "verifier_image_id": self.policy.document["verifier_image_id"],
            "source_session_id": candidate["source_session_id"],
            "source_artifact_id": candidate["source_artifact_id"],
            "isolation": {"network": "none"},
            "checks": [
                {"name": "trusted_requirement", "exit_code": 0, "test_count": 5,
                 "ok": True, "timed_out": False},
                {"name": "candidate_tests", "exit_code": 0, "test_count": 2,
                 "ok": True, "timed_out": False},
            ],
        }


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("initdb", "pg_ctl", "git"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="phase1e-pg-", dir="/private/tmp")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        cls.data = cls.root / "db"
        init = subprocess.run(["initdb", "-D", str(cls.data), "-A", "trust", "--no-instructions"],
                              capture_output=True, text=True, timeout=40)
        if init.returncode:
            raise RuntimeError(f"initdb failed: {init.stderr[-600:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={cls.socket} -c unix_socket_permissions=0700"
        start = subprocess.run(["pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                                "-o", options, "-w", "start"],
                               capture_output=True, text=True, timeout=40)
        if start.returncode:
            raise RuntimeError(f"pg_ctl failed: {start.stderr[-600:]}")
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
        name = f"publish_{tag}"
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.dsn = f"host={self.socket} dbname={name} user={getpass.getuser()}"
        self.database = Store(self.dsn)
        self.database.migrate()
        self.policy = Policy.load(HERE / "policy.json")
        self.artifacts = ContentAddressedStore(self.root / f"artifacts-{tag}")
        self.remote = self.root / f"remote-{tag}.git"
        cloned = subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(REPO_ROOT),
                                 str(self.remote)], capture_output=True, text=True, timeout=60)
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        self.task = f"PUB-{tag}"
        self.session = f"sess_{tag}"
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
        source = SavedTurnArtifact(self.session, f"turn_{tag}", f"artifact_{tag}", digest, archive)
        controller = Controller(self.database, self.policy, self.artifacts,
                                AcceptingVerifier(self.policy))
        controller.admit_candidate(self.task, source)
        verified = controller.resume(self.task)
        self.assertEqual(verified["state"], "VERIFIED")
        self.publisher = LocalBarePublisher(self.database, self.policy, self.artifacts, self.remote)

    def cli(self, *extra):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env["PHASE1C_DATABASE_URL"] = self.dsn
        return subprocess.run(
            [sys.executable, str(HERE / "publication_cli.py"), "--task", self.task,
             "--store-dir", str(self.artifacts.root), "--local-bare-remote", str(self.remote),
             *extra], cwd=HERE, env=env, capture_output=True, text=True, timeout=120)

    def test_process_restart_reconciles_exact_remote_commit(self):
        first = self.cli("--interrupt-after-push")
        self.assertEqual(first.returncode, 75, first.stderr)
        before = self.database.summary(self.task)
        self.assertEqual(before["publication_state"], "OUTCOME_UNKNOWN")
        second = self.cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        after = json.loads(second.stdout)
        self.assertEqual(after["publication_state"], "CONFIRMED")
        self.assertEqual(after["published_ref"], before["published_ref"])
        self.assertEqual(after["published_commit_sha"], before["published_commit_sha"])
        remote_commit = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "rev-parse", after["published_ref"]],
            text=True).strip()
        self.assertEqual(remote_commit, after["published_commit_sha"])
        parent = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "rev-parse", f"{remote_commit}^"],
            text=True).strip()
        self.assertEqual(parent, self.policy.document["base_commit"])
        diff = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "diff-tree", "--no-commit-id",
             "--name-only", "-r", parent, remote_commit], text=True).splitlines()
        self.assertEqual(set(diff), {
            "solutions/jira-to-pr/experiments/agents-api-spike/sample-project/sample/app.py",
            "solutions/jira-to-pr/experiments/agents-api-spike/sample-project/sample/tests/test_app.py",
        })
        run = self.database.get_run(self.task)
        candidate = self.database.get_candidate(run)
        _, accepted = read_candidate(self.artifacts.path(candidate["archive_sha256"]),
                                     candidate["archive_sha256"])
        for name in ALLOWED_FILES:
            published = subprocess.check_output(
                ["git", "--git-dir", str(self.remote), "show",
                 f"{remote_commit}:{BASE_PREFIX}{name}"])
            self.assertEqual(published, accepted[name])
        repeated = self.cli()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.database.summary(self.task)["published_commit_sha"], remote_commit)
        with self.database.connect() as conn:
            count = conn.execute("SELECT count(*) AS n FROM publication_attempts").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_uncertain_absent_ref_never_resends(self):
        original = self.publisher._git

        def reject_push(*args, **kwargs):
            if "push" in args:
                raise PublicationError("injected push failure")
            return original(*args, **kwargs)

        with mock.patch.object(self.publisher, "_git", side_effect=reject_push):
            with self.assertRaisesRegex(PublicationError, "injected"):
                self.publisher.publish(self.task)
        self.assertEqual(self.database.summary(self.task)["publication_state"], "OUTCOME_UNKNOWN")
        with self.assertRaisesRegex(PublicationError, "do not retry blindly"):
            self.publisher.publish(self.task)

    def test_tampered_artifact_blocks_publication(self):
        run = self.database.get_run(self.task)
        candidate = self.database.get_candidate(run)
        path = self.artifacts.path(candidate["archive_sha256"])
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(PublicationError, "integrity check failed"):
            self.publisher.publish(self.task)
        result = self.database.summary(self.task)
        self.assertEqual(result["state"], "NEEDS_HUMAN")
        self.assertIsNone(result["publication_state"])

    def test_remote_collision_and_competing_worker(self):
        run = self.database.get_run(self.task)
        with self.database.worker_lock(run["id"]):
            blocked = self.cli()
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("another worker", blocked.stderr)
        ref = f"refs/heads/agent/{run['id']}"
        subprocess.run(["git", "--git-dir", str(self.remote), "update-ref", ref,
                        run["base_commit"]], check=True)
        with self.assertRaisesRegex(PublicationError, "another commit"):
            self.publisher.publish(self.task)
        self.assertEqual(self.database.summary(self.task)["publication_state"], "PLANNED")

    def test_db_rejects_candidate_swap_and_intent_mutation(self):
        self.publisher.publish(self.task)
        row = self.database.get_publication(self.database.get_run(self.task)["id"])
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE publication_attempts SET candidate_id = %s WHERE id = %s",
                             (uuid.uuid4(), row["id"]))
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE publication_attempts SET state = 'OUTCOME_UNKNOWN', "
                             "confirmed_at = NULL WHERE id = %s", (row["id"],))
        different_remote = self.root / f"other-{uuid.uuid4().hex}.git"
        subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(REPO_ROOT),
                        str(different_remote)], capture_output=True, check=True)
        other = LocalBarePublisher(self.database, self.policy, self.artifacts, different_remote)
        with self.assertRaisesRegex(ValueError, "stored publication intent differs"):
            other.publish(self.task)

    def test_confirmed_remote_ref_drift_is_detected(self):
        self.publisher.publish(self.task)
        run = self.database.get_run(self.task)
        ref = f"refs/heads/agent/{run['id']}"
        subprocess.run(["git", "--git-dir", str(self.remote), "update-ref", ref,
                        run["base_commit"]], check=True)
        with self.assertRaisesRegex(PublicationError, "remote ref differs"):
            self.publisher.publish(self.task)
        self.assertEqual(self.database.summary(self.task)["publication_state"], "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
