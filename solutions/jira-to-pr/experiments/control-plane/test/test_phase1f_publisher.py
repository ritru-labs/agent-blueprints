"""Crash/restart proof for an updated Git branch under one durable draft PR."""

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

from adapters import ContentAddressedStore, REPO_ROOT, SavedTurnArtifact
from controller import Controller
from database import Store
from draft_pr import DraftPRCoordinator
from phase1f_observer import ObservationPolicy, TrustedPRObserver
from policy import Policy
from test_publication import AcceptingVerifier, SPIKE, HERE
from fake_github import FakeGitHub
from fake_git_transport import FakeGitTransport

BASE_REF = "refs/heads/qualification-base"


class CIRepublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("initdb", "pg_ctl", "git"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="phase1f-publish-", dir="/private/tmp")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        cls.data = cls.root / "db"
        init = subprocess.run(["initdb", "-D", str(cls.data), "-A", "trust", "--no-instructions"],
                              capture_output=True, text=True, timeout=40)
        if init.returncode:
            raise RuntimeError(f"initdb failed: {init.stderr[-400:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={cls.socket} -c unix_socket_permissions=0700"
        start = subprocess.run(["pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                                "-o", options, "-w", "start"], capture_output=True, text=True, timeout=40)
        if start.returncode:
            raise RuntimeError(f"pg_ctl failed: {start.stderr[-400:]}")
        cls.admin_dsn = f"host={cls.socket} dbname=postgres user={getpass.getuser()}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "data") and cls.data.exists():
            subprocess.run(["pg_ctl", "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
                           capture_output=True, timeout=40)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def archive(self, tag, assertion):
        archive = self.root / f"{tag}.zip"
        app = (SPIKE / "sample/app.py").read_text() + "\n\ndef add(a, b):\n    return a + b\n"
        tests = ((SPIKE / "sample/tests/test_app.py").read_text() +
                 "\nfrom sample.app import add\n\nclass AdditionTests(unittest.TestCase):\n"
                 f"    def test_add(self):\n        self.assertEqual({assertion})\n")
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sample/__init__.py", (SPIKE / "sample/__init__.py").read_bytes())
            bundle.writestr("sample/app.py", app)
            bundle.writestr("sample/tests/test_app.py", tests)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def setUp(self):
        tag = uuid.uuid4().hex[:12]
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(f"republish_{tag}")))
        self.dsn = f"host={self.socket} dbname=republish_{tag} user={getpass.getuser()}"
        self.database = Store(self.dsn)
        self.database.migrate()
        self.base = subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                                            text=True).strip()
        document = json.loads((HERE / "policy.json").read_text())
        document.update({"schema_version": 2, "base_commit": self.base,
                         "external_writes_enabled": True,
                         "allowed_operation_kinds": ["git_branch", "draft_pr"],
                         "github_target_base": BASE_REF,
                         "github_actor_login": "binnukyadari"})
        self.policy_path = self.root / f"policy-{tag}.json"
        self.policy_path.write_text(json.dumps(document))
        self.policy = Policy.load(self.policy_path)
        self.remote = self.root / f"remote-{tag}.git"
        subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(REPO_ROOT),
                        str(self.remote)], capture_output=True, check=True, timeout=60)
        subprocess.run(["git", "--git-dir", str(self.remote), "update-ref", BASE_REF, self.base],
                       capture_output=True, check=True)
        self.store = ContentAddressedStore(self.root / f"artifacts-{tag}")
        self.task = f"CI-PUBLISH-{tag}"
        self.session = f"sess_{tag}"
        self.fake_path = self.root / f"github-{tag}.json"
        self.fake_path.write_text(json.dumps({"actor_login": "binnukyadari",
                                              "refs": {BASE_REF: self.base},
                                              "trees": {}, "pulls": []}))
        self.fake = FakeGitHub(self.fake_path)
        self.publisher = FakeGitTransport(self.database, self.policy, self.store,
                                          self.remote, self.fake)
        self.controller = Controller(self.database, self.policy, self.store,
                                     AcceptingVerifier(self.policy))
        first, digest = self.archive(f"first-{tag}", "add(2, -3), -1")
        self.controller.admit_candidate(self.task, SavedTurnArtifact(
            self.session, f"turn_first_{tag}", f"artifact_first_{tag}", digest, first))
        self.assertEqual(self.controller.resume(self.task)["state"], "VERIFIED")
        self.publisher.publish(self.task)
        self.draft = DraftPRCoordinator(self.database, self.policy, self.store, self.fake)
        self.draft.create_or_reconcile(self.task)
        self.observation_policy = ObservationPolicy.from_document({
            "schema_version": 1, "required_checks": [{"name": "qualification",
                                                         "app_slug": "github-actions"}],
            "review_actors": [], "required_approvals": 0})
        self.observer = TrustedPRObserver(self.database, self.policy,
                                          self.observation_policy, self.fake)

    def check(self, head, check_id, conclusion):
        return {"id": check_id, "name": "qualification", "app": {"slug": "github-actions"},
                "head_sha": head, "status": "completed", "conclusion": conclusion,
                "started_at": "2026-09-27T04:00:00Z",
                "completed_at": "2026-09-27T04:01:00Z"}

    def worker(self, interrupt):
        env = dict(os.environ, PHASE1C_DATABASE_URL=self.dsn)
        env.pop("OPENAI_API_KEY", None)
        return subprocess.run(
            [sys.executable, str(HERE / "test/fake_git_worker.py"), self.task,
             str(self.policy_path), str(self.store.root), str(self.remote),
             str(self.fake_path), interrupt],
            cwd=HERE, env=env, capture_output=True, text=True, timeout=120)

    def test_repaired_branch_update_crash_reconciles_same_pr_and_new_ci(self):
        run = self.database.get_run(self.task)
        old_pub = self.database.get_publication(run["id"])
        state = self.fake._read()
        state["check_runs"] = {old_pub["commit_sha"]: [self.check(old_pub["commit_sha"], 1, "failure")]}
        self.fake._write(state)
        self.assertEqual(self.observer.observe(self.task)["gate"], "FAIL")
        intent = self.controller.plan_ci_repair(self.task)
        self.controller.mark_ci_repair_uncertain(self.task)
        self.controller.observe_ci_repair(self.task, self.session, intent["input_sha256"],
                                          "message_ci", "turn_ci")
        second, digest = self.archive(f"second-{self.task}", "add(-4, 6), 2")
        self.controller.admit_candidate(self.task, SavedTurnArtifact(
            self.session, "turn_ci", "artifact_ci", digest, second))
        self.assertEqual(self.controller.resume(self.task)["state"], "VERIFIED")
        first = self.worker("yes")
        self.assertEqual(first.returncode, 75, first.stderr)
        self.assertEqual(self.database.summary(self.task)["publication_state"], "OUTCOME_UNKNOWN")
        second_process = self.worker("no")
        self.assertEqual(second_process.returncode, 0, second_process.stderr)
        third = self.worker("no")
        self.assertEqual(third.returncode, 0, third.stderr)
        run = self.database.get_run(self.task)
        new_pub = self.database.get_publication(run["id"])
        self.assertNotEqual(old_pub["commit_sha"], new_pub["commit_sha"])
        parent = subprocess.check_output(["git", "--git-dir", str(self.remote),
                                          "rev-parse", f"{new_pub['commit_sha']}^"], text=True).strip()
        self.assertEqual(parent, old_pub["commit_sha"])
        self.assertEqual(self.fake.pull(1)["head"]["sha"], new_pub["commit_sha"])
        state = self.fake._read()
        state["check_runs"][new_pub["commit_sha"]] = [
            {**self.check(new_pub["commit_sha"], 2, None),
             "status": "in_progress", "completed_at": None}]
        self.fake._write(state)
        self.assertEqual(self.observer.observe(self.task)["gate"], "PENDING")
        state["check_runs"][new_pub["commit_sha"]] = [self.check(new_pub["commit_sha"], 2, "success")]
        self.fake._write(state)
        self.assertEqual(self.observer.observe(self.task)["gate"], "PASS")
        with self.database.connect() as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM candidate_artifacts WHERE run_id = %s) AS candidates, "
                "(SELECT count(*) FROM verification_runs WHERE run_id = %s) AS verifications, "
                "(SELECT count(*) FROM publication_attempts WHERE run_id = %s) AS publications, "
                "(SELECT count(*) FROM draft_pr_attempts WHERE run_id = %s) AS prs, "
                "(SELECT count(*) FROM pr_head_links WHERE run_id = %s) AS heads",
                (run["id"],) * 5).fetchone()
        self.assertEqual(tuple(counts.values()), (2, 2, 2, 1, 2))
