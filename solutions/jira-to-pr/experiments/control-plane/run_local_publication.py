"""Disposable Phase 1E proof: independent verification, push, death, reconcile."""

import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import uuid

from adapters import ContentAddressedStore, Phase1ASavedArtifact, Phase1BTrustedVerifier, REPO_ROOT
from controller import Controller
from database import Store
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parent
OUTPUT = HERE / ".control-runs/phase-1e-local-result.json"


def command(args, *, env, timeout=240):
    return subprocess.run(args, cwd=HERE, env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)


def main():
    for name in ("initdb", "pg_ctl", "git", "docker"):
        if shutil.which(name) is None:
            raise RuntimeError(f"{name} is required")
    source = Phase1ASavedArtifact()
    if not source.archive_path.is_file():
        raise RuntimeError("the locally downloaded Phase 1A ZIP is required")
    safe_env = dict(os.environ)
    safe_env.pop("OPENAI_API_KEY", None)
    with tempfile.TemporaryDirectory(prefix="phase1e-local-", dir="/private/tmp") as temporary:
        root = pathlib.Path(temporary)
        data = root / "db"
        socket = root / "socket"
        socket.mkdir(mode=0o700)
        initial = command(["initdb", "-D", str(data), "-A", "trust", "--no-instructions"],
                          env=safe_env, timeout=40)
        if initial.returncode:
            raise RuntimeError(f"initdb failed: {initial.stderr[-300:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={socket} -c unix_socket_permissions=0700"
        started = command(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                           "-o", options, "-w", "start"], env=safe_env, timeout=40)
        if started.returncode:
            raise RuntimeError(f"PostgreSQL start failed: {started.stderr[-300:]}")
        try:
            dsn = f"host={socket} dbname=postgres user={getpass.getuser()}"
            database = Store(dsn)
            database.migrate()
            policy = Policy.load(HERE / "policy.json")
            store = ContentAddressedStore(root / "artifacts")
            controller = Controller(database, policy, store, Phase1BTrustedVerifier())
            task = f"PHASE1E-{uuid.uuid4().hex[:12]}"
            controller.admit_candidate(task, source)
            verified = controller.resume(task)
            if verified["state"] != "VERIFIED" or verified["verification_status"] != "PASS":
                raise RuntimeError("independent Docker verification did not pass")
            bare = root / "remote.git"
            clone = command(["git", "clone", "--bare", "--no-hardlinks", str(REPO_ROOT), str(bare)],
                            env=safe_env, timeout=60)
            if clone.returncode:
                raise RuntimeError(f"disposable remote creation failed: {clone.stderr[-300:]}")
            publication_env = dict(safe_env, PHASE1C_DATABASE_URL=dsn)
            invocation = [sys.executable, str(HERE / "publication_cli.py"),
                          "--task", task, "--store-dir", str(store.root),
                          "--local-bare-remote", str(bare)]
            first = command([*invocation, "--interrupt-after-push"], env=publication_env)
            if first.returncode != 75:
                raise RuntimeError(f"publisher did not exit at the intended crash window: "
                                   f"{first.returncode} {first.stderr[-300:]}")
            interrupted = database.summary(task)
            if interrupted["publication_state"] != "OUTCOME_UNKNOWN":
                raise RuntimeError("PostgreSQL did not retain uncertain publication intent")
            second = command(invocation, env=publication_env)
            if second.returncode:
                raise RuntimeError(f"restart did not reconcile remote ref: {second.stderr[-300:]}")
            confirmed = database.summary(task)
            third = command(invocation, env=publication_env)
            if third.returncode or database.summary(task) != confirmed:
                raise RuntimeError("repeated publisher resume changed durable state")
            run = database.get_run(task)
            candidate = database.get_candidate(run)
            verification = database.get_verification(run)
            publication = database.get_publication(run["id"])
            if (confirmed["state"] != "VERIFIED" or
                    confirmed["publication_state"] != "CONFIRMED" or
                    publication["candidate_id"] != candidate["id"] or
                    publication["verification_id"] != verification["id"]):
                raise RuntimeError("publication is not linked to the current PASS candidate")
            remote_commit = command(["git", "--git-dir", str(bare), "rev-parse",
                                     publication["branch_ref"]], env=safe_env)
            remote_tree = command(["git", "--git-dir", str(bare), "rev-parse",
                                   f"{publication['commit_sha']}^{{tree}}"], env=safe_env)
            if (remote_commit.returncode or remote_tree.returncode or
                    remote_commit.stdout.strip() != publication["commit_sha"] or
                    remote_tree.stdout.strip() != publication["git_tree_sha"]):
                raise RuntimeError("remote commit/tree readback differs from durable intent")
            with database.connect() as conn:
                count = conn.execute("SELECT count(*) AS n FROM publication_attempts "
                                     "WHERE run_id = %s", (run["id"],)).fetchone()["n"]
            evidence = {
                "schema_version": 1, "phase": "1E-local-publisher", "status": "PASS",
                "remote_kind": "disposable_local_bare_git", "task_key": task,
                "run_id": str(run["id"]), "session_id": run["session_id"],
                "candidate_id": str(candidate["id"]),
                "verification_id": str(verification["id"]),
                "verification_status": verification["status"],
                "verifier_image_id": verification["verifier_image_id"],
                "artifact_sha256": candidate["archive_sha256"],
                "candidate_tree_sha256": candidate["tree_sha256"],
                "base_commit": run["base_commit"],
                "published_git_tree_sha": publication["git_tree_sha"],
                "published_commit_sha": publication["commit_sha"],
                "published_ref": publication["branch_ref"],
                "publisher_policy_hash": publication["publisher_policy_hash"],
                "process_restart": {"first_exit_code": 75,
                                    "state_before_restart": interrupted["publication_state"],
                                    "state_after_restart": confirmed["publication_state"],
                                    "repeated_resume_changed_state": False},
                "counts": {"candidates": confirmed["candidate_count"],
                           "verifications": confirmed["verification_count"],
                           "publication_attempts": count},
                "github_branch_pushed": False, "draft_pr_created": False,
            }
            OUTPUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            OUTPUT.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            print(f"Phase 1E local publisher PASS; evidence={OUTPUT}")
        finally:
            command(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                    env=safe_env, timeout=40)


if __name__ == "__main__":
    main()
