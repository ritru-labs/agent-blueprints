"""Persistent opt-in Phase 1E proof for one GitHub branch and draft PR.

The local PostgreSQL cluster and artifact store remain under --run-dir so an
uncertain external write can be reconciled on a later invocation.
"""

import argparse
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid

from adapters import ContentAddressedStore, Phase1ASavedArtifact, Phase1BTrustedVerifier, REPO_ROOT
from controller import Controller
from database import Store
from github_api import GitHubAPI, REPOSITORY
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / ".control-runs"


def command(args, *, env, timeout=240):
    return subprocess.run(args, cwd=HERE, env=env, capture_output=True,
                          text=True, timeout=timeout, check=False)


def start_pg(root, env):
    data = root / "db"
    socket = root / "socket"
    if not data.exists():
        socket.mkdir(mode=0o700)
        initial = command(["initdb", "-D", str(data), "-A", "trust", "--no-instructions"],
                          env=env, timeout=40)
        if initial.returncode:
            raise RuntimeError(f"initdb failed: {initial.stderr[-300:]}")
    else:
        socket.mkdir(mode=0o700, exist_ok=True)
    status = command(["pg_ctl", "-D", str(data), "status"], env=env, timeout=10)
    if status.returncode:
        options = f"-c listen_addresses='' -c unix_socket_directories={socket} -c unix_socket_permissions=0700"
        started = command(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                           "-o", options, "-w", "start"], env=env, timeout=40)
        if started.returncode:
            raise RuntimeError(f"PostgreSQL start failed: {started.stderr[-300:]}")
    return f"host={socket} dbname=postgres user={getpass.getuser()}"


def invoke_github(action, task, root, env, *, interrupt):
    args = [sys.executable, str(HERE / "github_cli.py"), action,
            "--task", task, "--store-dir", str(root / "artifacts"),
            "--policy", str(root / "policy.json")]
    if interrupt:
        args.append("--interrupt-after-write")
    return command(args, env=env, timeout=180)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=pathlib.Path,
                        default=RUNS / "github-live")
    parser.add_argument("--target-base-ref")
    parser.add_argument("--execute-github-writes", action="store_true")
    args = parser.parse_args()
    if not args.execute_github_writes:
        parser.error("--execute-github-writes is required for this public branch/PR proof")
    root = args.run_dir.resolve()
    if RUNS.resolve() not in root.parents or root == RUNS.resolve():
        parser.error("run directory must be a child of the ignored .control-runs directory")
    for name in ("initdb", "pg_ctl", "git", "docker", "gh"):
        if shutil.which(name) is None:
            raise RuntimeError(f"{name} is required")
    safe_env = dict(os.environ)
    safe_env.pop("OPENAI_API_KEY", None)
    github = GitHubAPI()
    if github.actor_login() != "binnukyadari":
        raise RuntimeError("active GitHub account is not binnukyadari")
    repository = github.repository()
    if repository.get("full_name") != REPOSITORY or \
            repository.get("permissions", {}).get("push") is not True:
        raise RuntimeError("active GitHub account cannot write the pinned repository")
    state_path = root / "run.json"
    if state_path.exists():
        if args.target_base_ref:
            parser.error("resuming a run cannot change its target base")
        state = json.loads(state_path.read_text())
        policy = Policy.load(root / "policy.json")
        if not state["task_key"].startswith("GH-DEMO-"):
            raise RuntimeError("unexpected saved task identity")
    else:
        if not args.target_base_ref:
            parser.error("--target-base-ref is required for a new run")
        if root.exists():
            raise RuntimeError("run directory already exists without a saved run identity")
        base = github.ref_sha(args.target_base_ref)
        if base is None:
            raise RuntimeError("target base branch does not exist")
        local = command(["git", "cat-file", "-t", base], env=safe_env)
        if local.returncode or local.stdout.strip() != "commit":
            raise RuntimeError("target base commit is absent from the trusted local repository")
        root.mkdir(mode=0o700, parents=True)
        document = json.loads((HERE / "policy.json").read_text())
        document.update({"schema_version": 2, "base_commit": base,
                         "external_writes_enabled": True,
                         "allowed_operation_kinds": ["git_branch", "draft_pr"],
                         "github_target_base": args.target_base_ref,
                         "github_actor_login": "binnukyadari"})
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        policy_path.chmod(0o600)
        policy = Policy.load(policy_path)
        state = {"task_key": f"GH-DEMO-{uuid.uuid4().hex[:12]}",
                 "target_base_ref": args.target_base_ref}
        state_path.write_text(json.dumps(state, sort_keys=True) + "\n")
        state_path.chmod(0o600)
    dsn = start_pg(root, safe_env)
    try:
        database = Store(dsn)
        database.migrate()
        task = state["task_key"]
        controller = Controller(database, policy, ContentAddressedStore(root / "artifacts"),
                                Phase1BTrustedVerifier())
        controller.admit_candidate(task, Phase1ASavedArtifact())
        verified = controller.resume(task)
        if verified["state"] != "VERIFIED" or verified["verification_status"] != "PASS":
            raise RuntimeError("fresh independent Docker verification did not pass")
        child_env = dict(safe_env, PHASE1C_DATABASE_URL=dsn)
        progress_path = root / "progress.json"
        progress = (json.loads(progress_path.read_text()) if progress_path.exists() else
                    {"push_exit_code": None, "draft_exit_code": None})
        run = database.get_run(task)
        publication = database.get_publication(run["id"])
        first_push_interrupted = publication is None
        push_first = invoke_github("publish", task, root, child_env,
                                   interrupt=first_push_interrupted)
        if first_push_interrupted and push_first.returncode != 75:
            raise RuntimeError(f"first publisher did not reach crash window: "
                               f"{push_first.returncode} {push_first.stderr[-300:]}")
        if not first_push_interrupted and push_first.returncode:
            raise RuntimeError(f"publisher recovery stopped: {push_first.stderr[-300:]}")
        if first_push_interrupted:
            progress["push_exit_code"] = push_first.returncode
            progress_path.write_text(json.dumps(progress, sort_keys=True) + "\n")
            progress_path.chmod(0o600)
        pushed = invoke_github("publish", task, root, child_env, interrupt=False)
        if pushed.returncode:
            raise RuntimeError(f"publisher reconciliation stopped: {pushed.stderr[-300:]}")
        run = database.get_run(task)
        draft = database.get_draft_pr(run["id"])
        first_pr_interrupted = draft is None
        pr_first = invoke_github("draft-pr", task, root, child_env,
                                 interrupt=first_pr_interrupted)
        if first_pr_interrupted and pr_first.returncode != 75:
            raise RuntimeError(f"first draft-PR process did not reach crash window: "
                               f"{pr_first.returncode} {pr_first.stderr[-300:]}")
        if not first_pr_interrupted and pr_first.returncode:
            raise RuntimeError(f"draft-PR recovery stopped: {pr_first.stderr[-300:]}")
        if first_pr_interrupted:
            progress["draft_exit_code"] = pr_first.returncode
            progress_path.write_text(json.dumps(progress, sort_keys=True) + "\n")
            progress_path.chmod(0o600)
        confirmed = invoke_github("draft-pr", task, root, child_env, interrupt=False)
        if confirmed.returncode:
            raise RuntimeError(f"draft-PR reconciliation stopped: {confirmed.stderr[-300:]}")
        repeated = invoke_github("draft-pr", task, root, child_env, interrupt=False)
        if repeated.returncode:
            raise RuntimeError(f"repeated draft-PR reconciliation stopped: {repeated.stderr[-300:]}")
        result = database.summary(task)
        if (result["state"], result["publication_state"], result["draft_pr_state"]) != \
                ("VERIFIED", "CONFIRMED", "CONFIRMED"):
            raise RuntimeError("database does not show the exact verified draft path")
        run = database.get_run(task)
        candidate = database.get_candidate(run)
        verification = database.get_verification(run)
        publication = database.get_publication(run["id"])
        draft = database.get_draft_pr(run["id"])
        with database.connect() as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM publication_attempts WHERE run_id = %s) AS publications, "
                "(SELECT count(*) FROM draft_pr_attempts WHERE run_id = %s) AS drafts",
                (run["id"], run["id"]),
            ).fetchone()
        if (publication["candidate_id"] != candidate["id"] or
                publication["verification_id"] != verification["id"] or
                draft["publication_id"] != publication["id"] or
                counts["publications"] != 1 or counts["drafts"] != 1 or
                github.ref_sha(publication["branch_ref"]) != publication["commit_sha"] or
                github.commit_tree_sha(publication["commit_sha"]) != publication["git_tree_sha"]):
            raise RuntimeError("remote or PostgreSQL exact-candidate linkage differs")
        evidence = {
            "schema_version": 1, "phase": "1E-GitHub-draft", "status": "PASS",
            "repository": REPOSITORY, "actor_login": "binnukyadari",
            "task_key": task, "run_id": str(run["id"]),
            "session_id": run["session_id"], "candidate_id": str(candidate["id"]),
            "verification_id": str(verification["id"]),
            "verification_status": verification["status"],
            "verifier_image_id": verification["verifier_image_id"],
            "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_ref": policy.document["github_target_base"],
            "base_commit": run["base_commit"],
            "published_ref": publication["branch_ref"],
            "published_commit_sha": publication["commit_sha"],
            "published_git_tree_sha": publication["git_tree_sha"],
            "draft_pr_number": draft["pr_number"],
            "draft_pr_url": draft["pr_url"],
            "publication_state": publication["state"],
            "draft_pr_state": draft["state"],
            "candidate_count": result["candidate_count"],
            "verification_count": result["verification_count"],
            "publication_count": counts["publications"],
            "draft_pr_count": counts["drafts"],
            "intentional_push_process_exit_code": progress["push_exit_code"],
            "intentional_draft_process_exit_code": progress["draft_exit_code"],
        }
        output = root / "result.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        output.chmod(0o600)
        print(json.dumps({"status": "PASS", "draft_pr_url": draft["pr_url"],
                          "evidence": str(output)}, sort_keys=True))
    finally:
        command(["pg_ctl", "-D", str(root / "db"), "-m", "fast", "-w", "stop"],
                env=safe_env, timeout=40)


if __name__ == "__main__":
    main()
