"""Real PostgreSQL, real process death, and a fresh-process recovery proof."""

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
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from adapters import PHASE1A, ContentAddressedStore, Phase1BTrustedVerifier
from controller import Controller
from database import Store
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("initdb", "pg_ctl", "docker"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is required for the integration test")
        source = PHASE1A / ".spike-runs/sample-project.zip"
        if not source.is_file():
            raise RuntimeError("local Phase 1A downloaded ZIP is required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="phase1c-pg-", dir="/private/tmp")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.data = cls.root / "db"
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        cls.store_dir = cls.root / "artifacts"
        cls.source_copy = cls.root / "source.zip"
        shutil.copyfile(source, cls.source_copy)
        cls.dsn = f"host={cls.socket} dbname=postgres user={getpass.getuser()}"
        init = subprocess.run(["initdb", "-D", str(cls.data), "-A", "trust", "--no-instructions"],
                              capture_output=True, text=True, timeout=40, check=False)
        if init.returncode:
            cls.temporary.cleanup()
            raise RuntimeError(f"initdb failed: {init.stderr[-1000:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={cls.socket} -c unix_socket_permissions=0700"
        start = subprocess.run(["pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                                "-o", options, "-w", "start"],
                               capture_output=True, text=True, timeout=40, check=False)
        if start.returncode:
            cls.temporary.cleanup()
            raise RuntimeError(f"pg_ctl start failed: {start.stderr[-1000:]}")
        cls.database = Store(cls.dsn)
        cls.database.migrate()
        cls.policy = Policy.load(HERE / "policy.json")
        with cls.database.connect() as connection:
            cls.server_version = connection.execute("SHOW server_version").fetchone()["server_version"]

    @classmethod
    def tearDownClass(cls):
        if all(hasattr(cls, name) for name in ("positive", "negative", "late_tamper")):
            output = HERE / ".control-runs/last-result.json"
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            output.write_text(json.dumps({
                "schema_version": 1, "phase": "1C", "status": "PASS",
                "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "postgres_server_version": cls.server_version,
                "positive_recovery": cls.positive,
                "negative_integrity": cls.negative,
                "post_verification_integrity": cls.late_tamper,
            }, indent=2, sort_keys=True) + "\n")
        if hasattr(cls, "data") and cls.data.exists():
            subprocess.run(["pg_ctl", "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
                           capture_output=True, timeout=40, check=False)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def cli(self, *arguments, store_dir=None):
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env["PHASE1C_DATABASE_URL"] = self.dsn
        return subprocess.run(
            [sys.executable, str(HERE / "cli.py"), "--store-dir",
             str(store_dir or self.store_dir), *arguments],
            cwd=HERE, env=env, capture_output=True, text=True, timeout=240, check=False,
        )

    def setUp(self):
        # A saved Agents session belongs to one workflow. Keep the original
        # Phase 1A session fixture, but give each independent test its own DB.
        db_name = f"phase1c_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(self.__class__.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        self.dsn = f"host={self.socket} dbname={db_name} user={getpass.getuser()}"
        self.database = Store(self.dsn)
        self.database.migrate()

    def test_real_process_restart_and_exact_candidate_link(self):
        first = self.cli("start", "--task", "DEMO-101", "--source-archive",
                         str(self.source_copy), "--interrupt-after-candidate")
        self.assertEqual(first.returncode, 75, first.stderr)
        self.assertIn("Intentional process exit", first.stdout)
        before = self.database.summary("DEMO-101")
        self.assertEqual(before["state"], "CANDIDATE_READY")
        self.assertEqual((before["candidate_count"], before["verification_count"], before["event_count"]),
                         (1, 0, 2))
        self.source_copy.unlink()

        with self.database.worker_lock(before["run_id"]):
            competing = self.cli("resume", "--task", "DEMO-101")
            self.assertEqual(competing.returncode, 1)
            self.assertIn("another worker", competing.stderr)
        self.assertEqual(self.database.summary("DEMO-101")["event_count"], 2)

        second = self.cli("resume", "--task", "DEMO-101")
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        after = json.loads(second.stdout)
        self.assertEqual(after["state"], "VERIFIED")
        self.assertEqual(after["candidate_id"], before["candidate_id"])
        self.assertEqual((after["candidate_count"], after["verification_count"], after["event_count"]),
                         (1, 1, 4))
        expected = json.loads((HERE.parent / "trusted-verifier/evidence/phase-1b-verification.json").read_text())
        self.assertEqual(after["artifact_sha256"], expected["source"]["artifact_sha256"])
        self.assertEqual(after["candidate_tree_sha256"], expected["candidate_tree_sha256"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT v.candidate_id, v.archive_sha256, v.tree_sha256, v.status, v.evidence "
                "FROM verification_runs v JOIN workflow_runs w ON w.verification_id = v.id "
                "WHERE w.task_key = 'DEMO-101'"
            ).fetchone()
        self.assertEqual(str(row["candidate_id"]), after["candidate_id"])
        self.assertEqual(row["archive_sha256"], after["artifact_sha256"])
        self.assertEqual(row["tree_sha256"], after["candidate_tree_sha256"])
        self.assertEqual((row["status"], row["evidence"]["status"]), ("PASS", "PASS"))

        third = self.cli("resume", "--task", "DEMO-101")
        self.assertEqual(third.returncode, 0, third.stderr)
        again = json.loads(third.stdout)
        self.assertEqual((again["candidate_id"], again["verification_id"], again["event_count"]),
                         (after["candidate_id"], after["verification_id"], after["event_count"]))

        controller = Controller(self.database, self.policy,
                                ContentAddressedStore(self.store_dir), Phase1BTrustedVerifier())
        operation = controller.reserve_synthetic_notice("DEMO-101", "status ready")
        same = controller.reserve_synthetic_notice("DEMO-101", "status ready")
        self.assertEqual(operation["id"], same["id"])
        self.assertEqual(self.database.mark_synthetic_operation_succeeded(operation["id"])["state"],
                         "SUCCEEDED")
        self.assertEqual(self.database.mark_synthetic_operation_succeeded(operation["id"])["state"],
                         "SUCCEEDED")
        with self.assertRaisesRegex(ValueError, "different intent"):
            controller.reserve_synthetic_notice("DEMO-101", "changed payload")
        unknown = self.database.reserve_synthetic_operation(
            self.database.get_run("DEMO-101")["id"],
            f"{after['run_id']}:synthetic_notice:uncertain",
            hashlib.sha256(b"uncertain delivery").hexdigest(),
        )
        self.assertEqual(self.database.mark_synthetic_operation_unknown(unknown["id"])["state"],
                         "OUTCOME_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "requires reconciliation"):
            self.database.mark_synthetic_operation_succeeded(unknown["id"])
        with self.database.connect() as connection:
            count = connection.execute("SELECT count(*) AS n FROM external_operations").fetchone()["n"]
        self.assertEqual(count, 2)

        with self.assertRaises(psycopg.Error):
            with self.database.connect() as connection:
                connection.execute("UPDATE candidate_artifacts SET tree_sha256 = %s WHERE id = %s",
                                   ("0" * 64, after["candidate_id"]))
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as connection:
                connection.execute("UPDATE verification_runs SET status = 'FAIL' WHERE id = %s",
                                   (after["verification_id"],))
        alternate_document = dict(self.policy.document, max_repair_attempts=1)
        alternate_path = self.root / "different-policy.json"
        alternate_path.write_text(json.dumps(alternate_document))
        alternate_policy = Policy.load(alternate_path)
        with self.assertRaisesRegex(ValueError, "policy differs"):
            Controller(self.database, alternate_policy, ContentAddressedStore(self.store_dir),
                       Phase1BTrustedVerifier()).resume("DEMO-101")
        self.assertEqual(self.database.summary("DEMO-101")["verification_count"], 1)
        self.__class__.positive = {
            "interrupted_process_exit_code": first.returncode,
            "state_after_interrupt": before["state"],
            "source_copy_removed_before_resume": True,
            "state_after_restart": after["state"],
            "run_id": after["run_id"],
            "candidate_id": after["candidate_id"],
            "verification_id": after["verification_id"],
            "artifact_sha256": after["artifact_sha256"],
            "candidate_tree_sha256": after["candidate_tree_sha256"],
            "candidate_count": after["candidate_count"],
            "verification_count": after["verification_count"],
            "event_count_after_repeated_resume": again["event_count"],
            "same_ids_after_repeated_resume": True,
            "competing_worker_blocked": True,
            "policy_drift_rejected": True,
            "same_idempotency_key_same_operation": True,
            "changed_payload_rejected": True,
            "unknown_outcome_retry_blocked": True,
            "candidate_and_verification_updates_rejected": True,
            "policy_sha256": after["policy_sha256"],
        }

    def test_tampered_durable_artifact_fails_closed(self):
        source = PHASE1A / ".spike-runs/sample-project.zip"
        private_source = self.root / "source-2.zip"
        shutil.copyfile(source, private_source)
        private_store = self.root / "artifacts-2"
        first = self.cli("start", "--task", "DEMO-102", "--source-archive",
                         str(private_source), "--interrupt-after-candidate", store_dir=private_store)
        self.assertEqual(first.returncode, 75, first.stderr)
        private_source.unlink()
        before = self.database.summary("DEMO-102")
        stored = ContentAddressedStore(private_store).path(before["artifact_sha256"])
        stored.write_bytes(stored.read_bytes() + b"tampered")
        resumed = self.cli("resume", "--task", "DEMO-102", store_dir=private_store)
        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        after = json.loads(resumed.stdout)
        self.assertEqual(after["state"], "NEEDS_HUMAN")
        self.assertEqual((after["candidate_count"], after["verification_count"]), (1, 0))
        run = self.database.get_run("DEMO-102")
        candidate = self.database.get_candidate(run)
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.database.connect() as connection:
                connection.execute("UPDATE workflow_runs SET state = 'VERIFYING' WHERE id = %s",
                                   (run["id"],))
        wrong_tree = "0" * 64
        forged = {
            "status": "PASS", "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": wrong_tree, "base_commit": candidate["base_commit"],
            "verifier_image_id": self.policy.document["verifier_image_id"],
        }
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO verification_runs(id, run_id, candidate_id, archive_sha256, tree_sha256, "
                    "base_commit, status, verifier_image_id, evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'PASS', %s, %s)",
                    (uuid.uuid4(), run["id"], candidate["id"], candidate["archive_sha256"],
                     wrong_tree, candidate["base_commit"],
                     self.policy.document["verifier_image_id"], Jsonb(forged)),
                )
        self.__class__.negative = {
            "interrupted_process_exit_code": first.returncode,
            "tampered_artifact_state_after_restart": after["state"],
            "verification_count": after["verification_count"],
            "illegal_transition_rejected": True,
            "forged_candidate_hash_link_rejected": True,
        }

    def test_verified_artifact_tamper_invalidates_gate(self):
        source = PHASE1A / ".spike-runs/sample-project.zip"
        private_source = self.root / "source-3.zip"
        shutil.copyfile(source, private_source)
        private_store = self.root / "artifacts-3"
        completed = self.cli("start", "--task", "DEMO-103", "--source-archive",
                             str(private_source), store_dir=private_store)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        before = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(before["state"], "VERIFIED")
        private_source.unlink()
        stored = ContentAddressedStore(private_store).path(before["artifact_sha256"])
        stored.write_bytes(stored.read_bytes() + b"changed after verification")
        resumed = self.cli("resume", "--task", "DEMO-103", store_dir=private_store)
        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        after = json.loads(resumed.stdout)
        self.assertEqual(after["state"], "NEEDS_HUMAN")
        self.assertEqual((after["candidate_count"], after["verification_count"]), (1, 1))
        self.assertEqual(after["verification_id"], before["verification_id"])
        controller = Controller(self.database, self.policy,
                                ContentAddressedStore(private_store), Phase1BTrustedVerifier())
        with self.assertRaisesRegex(ValueError, "requires a verified run"):
            controller.reserve_synthetic_notice("DEMO-103", "should not send")
        self.__class__.late_tamper = {
            "state_before_tamper": before["state"],
            "state_after_tamper": after["state"],
            "historical_verification_retained": after["verification_id"] == before["verification_id"],
            "verification_count": after["verification_count"],
            "new_operation_blocked": True,
        }


if __name__ == "__main__":
    unittest.main()
