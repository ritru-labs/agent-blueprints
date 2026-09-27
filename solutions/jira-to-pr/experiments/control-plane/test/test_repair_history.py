"""Adversarial Phase 1D state and lineage checks on a real PostgreSQL server."""

import getpass
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import uuid
import zipfile

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from adapters import ContentAddressedStore, SavedTurnArtifact
from controller import Controller
from database import BusyRun, Store
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parents[1]
SPIKE = HERE.parent / "agents-api-spike/sample-project"
FAILED_TEST = "FAIL: test_add_handles_positive_zero_and_negative_values (test_requirement.RequirementTests)\n"


class SyntheticVerifier:
    """Trusted-result-shaped fixture; the live proof uses the Docker verifier."""

    def __init__(self, policy, fail_ordinals=(1,)):
        self.policy = policy
        self.fail_ordinals = set(fail_ordinals)

    def verify(self, archive_path, candidate):
        fail = candidate["ordinal"] in self.fail_ordinals
        checks = [{"name": "trusted_requirement", "exit_code": 1 if fail else 0,
                   "test_count": 5, "ok": not fail, "timed_out": False,
                   "output_excerpt": FAILED_TEST if fail else "Ran 5 tests\nOK\n"}]
        if not fail:
            checks.append({"name": "candidate_tests", "exit_code": 0,
                           "test_count": 2, "ok": True, "timed_out": False,
                           "output_excerpt": "Ran 2 tests\nOK\n"})
        return {"status": "FAIL" if fail else "PASS",
                "artifact_sha256": candidate["archive_sha256"],
                "candidate_tree_sha256": candidate["tree_sha256"],
                "base_commit": candidate["base_commit"],
                "verifier_image_id": self.policy.document["verifier_image_id"],
                "source_session_id": candidate["source_session_id"],
                "source_artifact_id": candidate["source_artifact_id"],
                "isolation": {"network": "none"}, "checks": checks}


class RepairHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("initdb", "pg_ctl"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="phase1d-pg-", dir="/private/tmp")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        cls.data = cls.root / "db"
        init = subprocess.run(["initdb", "-D", str(cls.data), "-A", "trust", "--no-instructions"],
                              capture_output=True, text=True, timeout=40, check=False)
        if init.returncode:
            raise RuntimeError(f"initdb failed: {init.stderr[-600:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={cls.socket} -c unix_socket_permissions=0700"
        start = subprocess.run(["pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                                "-o", options, "-w", "start"],
                               capture_output=True, text=True, timeout=40, check=False)
        if start.returncode:
            raise RuntimeError(f"pg_ctl failed: {start.stderr[-600:]}")
        cls.admin_dsn = f"host={cls.socket} dbname=postgres user={getpass.getuser()}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "data") and cls.data.exists():
            subprocess.run(["pg_ctl", "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
                           capture_output=True, timeout=40, check=False)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def setUp(self):
        db_name = f"repair_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        self.dsn = f"host={self.socket} dbname={db_name} user={getpass.getuser()}"
        self.database = Store(self.dsn)
        self.database.migrate()
        self.policy = Policy.load(HERE / "policy.json")
        self.store = ContentAddressedStore(self.root / f"artifacts-{db_name}")
        self.controller = Controller(self.database, self.policy, self.store,
                                     SyntheticVerifier(self.policy))
        self.task = f"SYN-{db_name}"
        self.session = f"sess_{db_name}"

    def archive(self, ordinal):
        app = (SPIKE / "sample/app.py").read_text()
        tests = (SPIKE / "sample/tests/test_app.py").read_text()
        body = "return 0" if ordinal == 1 else "return a + b"
        app += f"\n\ndef add(a, b):\n    {body}\n"
        tests += ("\nfrom sample.app import add\n\nclass AdditionTests(unittest.TestCase):\n"
                  f"    def test_add(self):\n        self.assertEqual(add({ordinal}, 0), "
                  f"{0 if ordinal == 1 else ordinal})\n")
        archive = self.root / f"{self.task}-{ordinal}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sample/__init__.py", (SPIKE / "sample/__init__.py").read_bytes())
            bundle.writestr("sample/app.py", app)
            bundle.writestr("sample/tests/test_app.py", tests)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return SavedTurnArtifact(self.session, f"turn_{self.task}_{ordinal}",
                                 f"artifact_{self.task}_{ordinal}", digest, archive)

    def first_failure(self):
        self.controller.admit_candidate(self.task, self.archive(1))
        state = self.controller.resume(self.task)
        self.assertEqual(state["state"], "REPAIR_PENDING")
        self.assertEqual((state["candidate_count"], state["verification_count"]), (1, 1))
        return state

    def observed_repair(self):
        first = self.first_failure()
        intent = self.controller.plan_repair(self.task)
        self.assertEqual(hashlib.sha256(intent["message"].encode()).hexdigest(),
                         intent["input_sha256"])
        self.controller.mark_repair_uncertain(self.task)
        self.controller.observe_repair(self.task, self.session, intent["input_sha256"],
                                       f"msg_{self.task}", f"turn_{self.task}_2")
        return first, intent

    def test_exact_history_and_duplicate_recovery(self):
        first, intent = self.observed_repair()
        self.assertEqual(self.controller.plan_repair(self.task)["input_key"], intent["input_key"])
        again = self.controller.observe_repair(self.task, self.session, intent["input_sha256"],
                                               f"msg_{self.task}", f"turn_{self.task}_2")
        self.assertEqual(again["ordinal"], 1)
        second = self.controller.admit_candidate(self.task, self.archive(2))
        self.assertEqual(second["candidate_ordinal"], 2)
        final = self.controller.resume(self.task)
        self.assertEqual(final["state"], "VERIFIED")
        self.assertEqual((final["candidate_count"], final["verification_count"],
                          final["repair_count"], final["session_count"]), (2, 2, 1, 1))
        self.assertEqual(self.controller.resume(self.task), final)
        history = self.database.history(self.task)
        self.assertEqual([c["ordinal"] for c in history["candidates"]], [1, 2])
        self.assertNotEqual(history["candidates"][0]["archive_sha256"],
                            history["candidates"][1]["archive_sha256"])
        self.assertNotEqual(history["candidates"][0]["tree_sha256"],
                            history["candidates"][1]["tree_sha256"])
        self.assertEqual([v["status"] for v in history["verifications"]], ["FAIL", "PASS"])
        self.assertEqual(history["repairs"][0]["failed_verification_id"],
                         history["verifications"][0]["id"])
        self.assertEqual(history["candidates"][1]["repair_attempt_id"],
                         history["repairs"][0]["id"])
        self.assertEqual(history["verifications"][1]["candidate_id"],
                         history["candidates"][1]["id"])
        self.assertNotIn("output_excerpt", json.dumps(history))

    def test_stale_result_candidate_swap_and_wrong_turn_fail(self):
        first, intent = self.observed_repair()
        wrong = self.archive(2)
        with self.assertRaisesRegex(ValueError, "reconciled repair turn"):
            self.controller.admit_candidate(self.task, SavedTurnArtifact(
                self.session, "turn_unrelated", wrong.artifact_id, wrong.archive_sha256,
                wrong.archive_path))
        original = self.archive(1)
        with self.assertRaises(psycopg.Error):
            self.controller.admit_candidate(self.task, SavedTurnArtifact(
                self.session, f"turn_{self.task}_2", f"artifact_{self.task}_same_tree",
                original.archive_sha256, original.archive_path))
        self.controller.admit_candidate(self.task, wrong)
        current = self.database.get_run(self.task)
        old = self.database.history(self.task)["verifications"][0]
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE workflow_runs SET state = 'VERIFIED', verification_id = %s "
                             "WHERE id = %s", (old["id"], current["id"]))
        self.database.begin_verification(current["id"])
        candidate = self.database.get_candidate(current)
        forged = dict(old["evidence"], status="PASS")
        with self.assertRaises(psycopg.Error):
            self.database.finish_verification(current["id"], candidate, forged)
        self.controller.resume(self.task)
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE workflow_runs SET candidate_id = %s WHERE id = %s",
                             (first["candidate_id"], current["id"]))

    def test_budget_and_policy_drift_are_enforced(self):
        policy_path = self.root / f"{self.task}-policy.json"
        policy_path.write_text(json.dumps(dict(self.policy.document, max_repair_attempts=1)))
        self.policy = Policy.load(policy_path)
        self.controller = Controller(self.database, self.policy, self.store,
                                     SyntheticVerifier(self.policy, fail_ordinals=(1, 2)))
        first, intent = self.observed_repair()
        self.controller.admit_candidate(self.task, self.archive(2))
        exhausted = self.controller.resume(self.task)
        self.assertEqual(exhausted["state"], "NEEDS_HUMAN")
        self.assertEqual(exhausted["repair_count"], 1)
        with self.assertRaises(ValueError):
            self.controller.plan_repair(self.task)
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("INSERT INTO repair_attempts(id,run_id,ordinal,failed_candidate_id,"
                             "failed_verification_id,session_id,input_key,input_sha256) "
                             "VALUES (%s,%s,2,%s,%s,%s,%s,%s)",
                             (uuid.uuid4(), exhausted["run_id"], exhausted["candidate_id"],
                              exhausted["verification_id"], self.session, "0" * 64, "1" * 64))
        different = Controller(self.database, Policy.load(HERE / "policy.json"), self.store,
                               SyntheticVerifier(self.policy))
        with self.assertRaisesRegex(ValueError, "policy differs"):
            different.resume(self.task)

    def test_second_policy_repair_creates_candidate_three(self):
        self.controller = Controller(self.database, self.policy, self.store,
                                     SyntheticVerifier(self.policy, fail_ordinals=(1, 2)))
        self.observed_repair()
        self.controller.admit_candidate(self.task, self.archive(2))
        second_fail = self.controller.resume(self.task)
        self.assertEqual(second_fail["state"], "REPAIR_PENDING")
        intent = self.controller.plan_repair(self.task)
        self.assertEqual(intent["ordinal"], 2)
        self.controller.mark_repair_uncertain(self.task)
        self.controller.observe_repair(self.task, self.session, intent["input_sha256"],
                                       f"msg_{self.task}_2", f"turn_{self.task}_3")
        self.controller.admit_candidate(self.task, self.archive(3))
        final = self.controller.resume(self.task)
        self.assertEqual(final["state"], "VERIFIED")
        self.assertEqual((final["candidate_count"], final["verification_count"],
                          final["repair_count"], final["candidate_ordinal"]), (3, 3, 2, 3))

    def test_conflicting_session_identity_unknown_message_and_competing_worker(self):
        first = self.first_failure()
        with self.assertRaises(ValueError):
            self.database.create_run(self.task, "sess_conflicting", self.policy)
        with self.assertRaises(psycopg.Error):
            self.database.create_run(self.task + "-other", self.session, self.policy)
        with self.assertRaises(ValueError):
            self.database.replace_session(first["run_id"], "sess_wrong", "sess_replacement")
        with self.assertRaises(psycopg.Error):
            with self.database.connect() as conn:
                conn.execute("UPDATE workflow_runs SET state = 'REPAIR_INPUT_PLANNED' WHERE id = %s",
                             (first["run_id"],))
        with self.database.worker_lock(first["run_id"]):
            with self.assertRaises(BusyRun):
                self.controller.plan_repair(self.task)
        intent = self.controller.plan_repair(self.task)
        self.assertEqual(self.controller.plan_repair(self.task)["input_key"], intent["input_key"])
        self.controller.mark_repair_uncertain(self.task)
        self.controller.mark_repair_uncertain(self.task)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.controller.observe_repair(self.task, self.session, "0" * 64,
                                           "msg_wrong", f"turn_{self.task}_2")
        self.assertEqual(self.database.summary(self.task)["state"], "REPAIR_INPUT_UNKNOWN")
        with self.assertRaises(ValueError):
            self.controller.admit_candidate(self.task, self.archive(2))

    def test_replacement_session_lineage_and_tampered_artifact(self):
        first = self.first_failure()
        replacement = f"sess_replacement_{self.task}"
        self.database.replace_session(first["run_id"], self.session, replacement)
        state = self.database.summary(self.task)
        self.assertEqual(state["current_session_id"], replacement)
        self.assertEqual(self.database.history(self.task)["sessions"][1]["predecessor_session_id"],
                         self.session)
        with self.assertRaises(ValueError):
            self.controller.admit_candidate(self.task, self.archive(2))
        stored = self.store.path(first["artifact_sha256"])
        stored.write_bytes(stored.read_bytes() + b"tamper")
        state = self.controller.resume(self.task)
        self.assertEqual(state["state"], "NEEDS_HUMAN")
        self.assertEqual((state["candidate_count"], state["verification_count"]), (1, 1))

    def test_candidate_changed_during_verifier_is_not_committed(self):
        class MutatingVerifier(SyntheticVerifier):
            def verify(self, archive_path, candidate):
                result = super().verify(archive_path, candidate)
                archive_path.write_bytes(archive_path.read_bytes() + b"changed during check")
                return result

        self.controller = Controller(self.database, self.policy, self.store,
                                     MutatingVerifier(self.policy))
        self.controller.admit_candidate(self.task, self.archive(1))
        result = self.controller.resume(self.task)
        self.assertEqual(result["state"], "NEEDS_HUMAN")
        self.assertEqual(result["verification_count"], 0)

    def test_version_one_candidate_survives_version_two_migration(self):
        db_name = f"legacy_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        legacy = Store(f"host={self.socket} dbname={db_name} user={getpass.getuser()}")
        run_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        verification_id = uuid.uuid4()
        session_id = f"sess_{db_name}"
        with legacy.connect() as conn:
            conn.execute((HERE / "schema.sql").read_text(), prepare=False)
            conn.execute("INSERT INTO policy_snapshots(policy_hash, document) VALUES (%s, %s)",
                         (self.policy.sha256, Jsonb(self.policy.document)))
            conn.execute("INSERT INTO workflow_runs(id,task_key,repository,base_commit,session_id,policy_hash) "
                         "VALUES (%s,%s,%s,%s,%s,%s)",
                         (run_id, db_name, self.policy.document["repository"],
                          self.policy.document["base_commit"], session_id, self.policy.sha256))
            conn.execute("INSERT INTO candidate_artifacts(id,run_id,source_artifact_id,"
                         "archive_sha256,tree_sha256,base_commit,storage_path) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (candidate_id, run_id, "artifact_legacy", "a" * 64, "b" * 64,
                          self.policy.document["base_commit"], "legacy.zip"))
            conn.execute("UPDATE workflow_runs SET state = 'CANDIDATE_READY', candidate_id = %s "
                         "WHERE id = %s", (candidate_id, run_id))
            conn.execute("UPDATE workflow_runs SET state = 'VERIFYING' WHERE id = %s", (run_id,))
            evidence = {"status": "PASS", "artifact_sha256": "a" * 64,
                        "candidate_tree_sha256": "b" * 64,
                        "base_commit": self.policy.document["base_commit"],
                        "verifier_image_id": self.policy.document["verifier_image_id"]}
            conn.execute("INSERT INTO verification_runs(id,run_id,candidate_id,archive_sha256,"
                         "tree_sha256,base_commit,status,verifier_image_id,evidence) "
                         "VALUES (%s,%s,%s,%s,%s,%s,'PASS',%s,%s)",
                         (verification_id, run_id, candidate_id, "a" * 64, "b" * 64,
                          self.policy.document["base_commit"],
                          self.policy.document["verifier_image_id"], Jsonb(evidence)))
            conn.execute("UPDATE workflow_runs SET state = 'VERIFIED', verification_id = %s "
                         "WHERE id = %s", (verification_id, run_id))
        legacy.migrate()
        legacy.migrate()
        history = legacy.history(db_name)
        self.assertEqual(history["run"]["state"], "VERIFIED")
        self.assertEqual(history["run"]["current_session_id"], session_id)
        self.assertEqual(history["candidates"][0]["id"], str(candidate_id))
        self.assertEqual(history["candidates"][0]["ordinal"], 1)
        self.assertEqual(history["candidates"][0]["source_session_id"], session_id)
        self.assertIsNone(history["candidates"][0]["source_turn_id"])
        self.assertEqual(history["verifications"][0]["id"], str(verification_id))
        self.assertEqual(history["verifications"][0]["candidate_id"], str(candidate_id))
        with legacy.connect() as conn:
            versions = conn.execute("SELECT array_agg(version ORDER BY version) AS versions "
                                    "FROM schema_migrations").fetchone()["versions"]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8, 9])


if __name__ == "__main__":
    unittest.main()
