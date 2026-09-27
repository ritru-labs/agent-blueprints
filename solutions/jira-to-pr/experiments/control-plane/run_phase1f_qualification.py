"""Resumable, scoped live Phase 1F proof on a fresh synthetic draft PR.

Each stage is a separate process. PostgreSQL is stopped after every stage so
the next invocation proves restart recovery. No Jira or merge operation exists.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import uuid

from adapters import ContentAddressedStore
from archive_intake import read_candidate
from database import Store
from github_api import GitHubAPI, REPOSITORY
from phase1f_observer import ObservationPolicy, TrustedPRObserver
from policy import Policy
from run_github_draft import start_pg

HERE = pathlib.Path(__file__).resolve().parent
SPIKE = HERE.parent / "agents-api-spike"
ROOT = HERE / ".control-runs/phase1f-live"
BASE_REF = "refs/heads/feat/jira-to-pr-v0.1"
OBSERVATION_DOCUMENT = {
    "schema_version": 1,
    "required_checks": [{"name": "qualification", "app_slug": "github-actions",
                         "repair_code": "REMOVE_PHASE1F_MARKER"}],
    "review_actors": ["binnukyadari"],
    "required_approvals": 0,
}


def safe_environment(dsn):
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    if dsn is not None:
        env["PHASE1C_DATABASE_URL"] = dsn
    env.update(PHASE1D_POLICY_PATH=str(ROOT / "policy.json"),
               PHASE1F_OBSERVATION_POLICY_PATH=str(ROOT / "observation-policy.json"),
               PHASE1D_STORE_DIR=str(ROOT / "artifacts"),
               PHASE1D_RUN_DIR=str(ROOT / "downloads"))
    return env


def child(args, *, env, cwd=HERE, timeout=600):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout, check=False)


def check_child(result, label, expected=0):
    if result.returncode != expected:
        raise RuntimeError(f"{label} stopped with exit {result.returncode}; "
                           f"local stderr tail: {result.stderr[-400:]}")
    return result


def github_child(action, task, env, interrupt=False):
    args = [sys.executable, str(HERE / "github_cli.py"), action,
            "--task", task, "--store-dir", str(ROOT / "artifacts"),
            "--policy", str(ROOT / "policy.json")]
    if interrupt:
        args.append("--interrupt-after-write")
    return child(args, env=env, timeout=240)


def node_child(script, task, env, command=None):
    args = ["node", "--env-file=.env.local", f"repair-loop/{script}"]
    if command:
        args.append(command)
    args.extend(["--task", task])
    return child(args, env=env, cwd=SPIKE, timeout=900)


def save_private(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def initialize():
    if ROOT.exists():
        state = json.loads((ROOT / "run.json").read_text())
        return state
    github = GitHubAPI()
    if github.actor_login() != "binnukyadari":
        raise RuntimeError("active GitHub account differs from scoped policy")
    repository = github.repository()
    if repository.get("full_name") != REPOSITORY or \
            repository.get("permissions", {}).get("push") is not True:
        raise RuntimeError("GitHub account lacks pinned repository push permission")
    base = github.ref_sha(BASE_REF)
    if base is None:
        raise RuntimeError("qualification base branch is absent")
    local = subprocess.run(["git", "cat-file", "-t", base], capture_output=True,
                           text=True, timeout=10, check=False)
    if local.returncode or local.stdout.strip() != "commit":
        raise RuntimeError("qualification base is not present in trusted local Git")
    workflow = subprocess.run(["git", "show", f"{base}:.github/workflows/phase-1f-qualification.yml"],
                              capture_output=True, text=True, timeout=10, check=False)
    if workflow.returncode or "agent/**" not in workflow.stdout:
        raise RuntimeError("pinned base lacks the synthetic head-SHA qualification workflow")
    ROOT.mkdir(mode=0o700, parents=True)
    document = json.loads((HERE / "policy.json").read_text())
    document.update({"schema_version": 2, "base_commit": base,
                     "external_writes_enabled": True,
                     "allowed_operation_kinds": ["git_branch", "draft_pr"],
                     "github_target_base": BASE_REF,
                     "github_actor_login": "binnukyadari"})
    save_private(ROOT / "policy.json", document)
    save_private(ROOT / "observation-policy.json", OBSERVATION_DOCUMENT)
    state = {"task_key": f"GH-CI-{uuid.uuid4().hex[:12]}", "base_commit": base,
             "base_ref": BASE_REF}
    save_private(ROOT / "run.json", state)
    return state


def progress():
    path = ROOT / "progress.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_progress(value):
    save_private(ROOT / "progress.json", value)


def stage_initial(database, task, env):
    try:
        run = database.get_run(task)
    except KeyError:
        result = node_child("phase1f_qualification.mjs", task, env)
        check_child(result, "initial Agents API candidate")
        run = database.get_run(task)
    if run["state"] != "VERIFIED":
        raise RuntimeError("initial candidate did not reach trusted VERIFIED state")
    candidate = database.get_candidate(run)
    archive = ContentAddressedStore(ROOT / "artifacts").checked_path(
        candidate["archive_sha256"], candidate["storage_path"])
    _, accepted = read_candidate(archive, candidate["archive_sha256"])
    if b"PHASE1F_REPAIR_REQUIRED" not in accepted["sample/app.py"]:
        raise RuntimeError("initial candidate lacks the intentional CI failure marker")
    check_child(github_child("publish", task, env), "initial GitHub publication")
    check_child(github_child("draft-pr", task, env), "initial draft PR")
    result = database.summary(task)
    print(json.dumps({"stage": "initial", "task_key": task, "pr_url": result["draft_pr_url"],
                      "head_commit_sha": result["published_commit_sha"],
                      "candidate_count": result["candidate_count"]}, sort_keys=True))


def repin_unstarted_run(database, state):
    try:
        database.get_run(state["task_key"])
        return state
    except KeyError:
        pass
    current = GitHubAPI().ref_sha(BASE_REF)
    if current is None:
        raise RuntimeError("qualification base branch disappeared")
    if current == state["base_commit"]:
        return state
    local = subprocess.run(["git", "cat-file", "-t", current], capture_output=True,
                           text=True, timeout=10, check=False)
    workflow = subprocess.run(["git", "show", f"{current}:.github/workflows/phase-1f-qualification.yml"],
                              capture_output=True, text=True, timeout=10, check=False)
    if local.returncode or local.stdout.strip() != "commit" or workflow.returncode:
        raise RuntimeError("new qualification base is not present with the trusted CI workflow")
    policy = json.loads((ROOT / "policy.json").read_text())
    policy["base_commit"] = current
    save_private(ROOT / "policy.json", policy)
    state["base_commit"] = current
    save_private(ROOT / "run.json", state)
    return state


def stage_observe(database, task):
    observer = TrustedPRObserver(
        database, Policy.load(ROOT / "policy.json"),
        ObservationPolicy.from_document(json.loads((ROOT / "observation-policy.json").read_text())),
        GitHubAPI(),
    )
    batch = observer.observe(task)
    print(json.dumps({"stage": "observe", "observation_id": str(batch["id"]),
                      "head_commit_sha": batch["head_commit_sha"],
                      "gate": batch["gate"], "findings": batch["findings"]}, sort_keys=True))


def stage_repair_submit(database, task, env):
    batch = database.latest_pr_observation(database.get_run(task)["id"])
    if batch is None or batch["gate"] != "FAIL":
        raise RuntimeError("exact-head CI failure has not been durably observed")
    result = node_child("phase1f.mjs", task, env, "run")
    check_child(result, "CI repair submission crash proof", expected=75)
    value = progress()
    value["repair_submission_exit_code"] = result.returncode
    save_progress(value)
    print(json.dumps({"stage": "repair-submit", "exit_code": 75,
                      "repair_status": database.current_ci_repair(database.get_run(task)["id"])["status"]}))


def stage_repair_resume(database, task, env):
    result = node_child("phase1f.mjs", task, env, "resume")
    check_child(result, "CI repair process restart")
    state = database.summary(task)
    if state["state"] != "VERIFIED" or state["candidate_count"] != 2 or \
            state["verification_count"] != 2:
        raise RuntimeError("repair did not produce one new independently verified candidate")
    print(json.dumps({"stage": "repair-resume", "state": state["state"],
                      "candidate_count": state["candidate_count"],
                      "verification_count": state["verification_count"]}, sort_keys=True))


def stage_publish_submit(database, task, env):
    state = database.summary(task)
    if state["state"] != "VERIFIED" or state["candidate_count"] != 2:
        raise RuntimeError("repaired candidate is not independently verified")
    result = github_child("publish", task, env, interrupt=True)
    check_child(result, "repaired branch update crash proof", expected=75)
    value = progress()
    value["branch_update_exit_code"] = result.returncode
    save_progress(value)
    print(json.dumps({"stage": "publish-submit", "exit_code": 75,
                      "publication_state": database.summary(task)["publication_state"]}))


def stage_publish_resume(database, task, env):
    check_child(github_child("publish", task, env), "repaired branch readback")
    check_child(github_child("publish", task, env), "repeated repaired branch reconciliation")
    run = database.get_run(task)
    draft = database.get_draft_pr(run["id"])
    publication = database.get_publication(run["id"])
    github = GitHubAPI()
    pull = github.pull(draft["pr_number"])
    if pull["head"]["sha"] != publication["commit_sha"] or \
            github.ref_sha(publication["branch_ref"]) != publication["commit_sha"] or \
            github.commit_tree_sha(publication["commit_sha"]) != publication["git_tree_sha"]:
        raise RuntimeError("GitHub PR, branch, or tree readback differs")
    print(json.dumps({"stage": "publish-resume", "pr_number": draft["pr_number"],
                      "new_head_commit_sha": publication["commit_sha"],
                      "publication_state": publication["state"]}, sort_keys=True))


def stage_body_sync(database, task, env):
    check_child(github_child("sync-pr-body", task, env), "same-PR body reconciliation")
    check_child(github_child("sync-pr-body", task, env), "repeated same-PR body reconciliation")
    run = database.get_run(task)
    head = database.latest_pr_head(run["id"])
    row = database.get_pr_body_update(head["id"])
    if row is None or row["state"] != "CONFIRMED":
        raise RuntimeError("current PR body has no confirmed exact-head readback")
    print(json.dumps({"stage": "body-sync", "head_commit_sha": head["head_commit_sha"],
                      "body_update_state": row["state"]}, sort_keys=True))


def stage_evidence(database, task):
    run = database.get_run(task)
    with database.connect() as conn:
        candidates = conn.execute("SELECT * FROM candidate_artifacts WHERE run_id = %s "
                                  "ORDER BY ordinal", (run["id"],)).fetchall()
        verifications = conn.execute("SELECT * FROM verification_runs WHERE run_id = %s "
                                     "ORDER BY created_at, id", (run["id"],)).fetchall()
        publications = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s "
                                    "ORDER BY created_at, id", (run["id"],)).fetchall()
        heads = conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s "
                             "ORDER BY ordinal", (run["id"],)).fetchall()
        observations = conn.execute("SELECT * FROM pr_observation_batches WHERE run_id = %s "
                                    "ORDER BY observed_at, id", (run["id"],)).fetchall()
        repairs = conn.execute("SELECT * FROM ci_repair_intents WHERE run_id = %s",
                               (run["id"],)).fetchall()
        drafts = conn.execute("SELECT * FROM draft_pr_attempts WHERE run_id = %s",
                              (run["id"],)).fetchall()
    if (len(candidates), len(verifications), len(publications), len(heads),
            len(repairs), len(drafts)) != (2, 2, 2, 2, 1, 1):
        raise RuntimeError("Phase 1F durable lineage has duplicate or missing records")
    body_update = database.get_pr_body_update(heads[-1]["id"])
    if body_update is None or body_update["state"] != "CONFIRMED":
        raise RuntimeError("current PR body update lacks exact-head confirmation")
    if ([v["status"] for v in verifications] != ["PASS", "PASS"] or
            [h["head_commit_sha"] for h in heads] != [p["commit_sha"] for p in publications] or
            any(c["id"] != v["candidate_id"] or c["id"] != p["candidate_id"] or
                v["id"] != p["verification_id"] for c, v, p in
                zip(candidates, verifications, publications)) or
            candidates[1]["ci_repair_id"] != repairs[0]["id"] or
            repairs[0]["status"] != "OBSERVED" or
            len({c["archive_sha256"] for c in candidates}) != 2 or
            len({c["tree_sha256"] for c in candidates}) != 2 or
            not any(o["head_commit_sha"] == heads[0]["head_commit_sha"] and o["gate"] == "FAIL"
                    for o in observations) or
            not any(o["head_commit_sha"] == heads[1]["head_commit_sha"] and o["gate"] == "PASS"
                    for o in observations)):
        raise RuntimeError("candidate, publication, or exact-head CI lineage is incomplete")
    github = GitHubAPI()
    pull = github.pull(drafts[0]["pr_number"])
    if pull["head"]["sha"] != heads[1]["head_commit_sha"] or \
            github.ref_sha(publications[1]["branch_ref"]) != publications[1]["commit_sha"] or \
            github.commit_tree_sha(publications[1]["commit_sha"]) != publications[1]["git_tree_sha"]:
        raise RuntimeError("live GitHub readback differs from final durable head")
    value = {
        "schema_version": 1, "phase": "1F", "status": "PASS",
        "repository": REPOSITORY, "run_id": str(run["id"]),
        "session_id": run["session_id"], "draft_pr_number": drafts[0]["pr_number"],
        "draft_pr_url": drafts[0]["pr_url"], "base_commit": run["base_commit"],
        "candidate_ids": [str(c["id"]) for c in candidates],
        "candidate_artifact_sha256": [c["archive_sha256"] for c in candidates],
        "candidate_tree_sha256": [c["tree_sha256"] for c in candidates],
        "verification_ids": [str(v["id"]) for v in verifications],
        "verification_statuses": [v["status"] for v in verifications],
        "publication_ids": [str(p["id"]) for p in publications],
        "head_commit_sha": [h["head_commit_sha"] for h in heads],
        "ci_repair_id": str(repairs[0]["id"]),
        "ci_repair_status": repairs[0]["status"],
        "pr_body_update_id": str(body_update["id"]),
        "pr_body_update_state": body_update["state"],
        "observation_gates_by_head": [
            {"head_commit_sha": h["head_commit_sha"],
             "gates": [o["gate"] for o in observations if o["head_commit_sha"] == h["head_commit_sha"]]}
            for h in heads],
        "process_restarts": progress(),
        "counts": {"candidates": 2, "verifications": 2, "publications": 2,
                   "pr_heads": 2, "draft_prs": 1, "ci_repairs": 1},
        "automatic_merge": False,
    }
    save_private(ROOT / "phase1f-result.json", value)
    print(json.dumps({"stage": "evidence", "status": "PASS",
                      "pr_url": value["draft_pr_url"],
                      "evidence": str(ROOT / "phase1f-result.json")}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("initial", "observe", "repair-submit",
                                          "repair-resume", "publish-submit", "publish-resume",
                                          "body-sync",
                                          "evidence"))
    args = parser.parse_args()
    if args.stage != "initial" and not (ROOT / "run.json").exists():
        raise RuntimeError("Phase 1F qualification run has not been initialized")
    state = initialize() if args.stage == "initial" else json.loads((ROOT / "run.json").read_text())
    env = safe_environment(None)
    dsn = start_pg(ROOT, env)
    env = safe_environment(dsn)
    try:
        database = Store(dsn)
        database.migrate()
        if args.stage == "initial":
            state = repin_unstarted_run(database, state)
        task = state["task_key"]
        if args.stage == "initial":
            stage_initial(database, task, env)
        elif args.stage == "observe":
            stage_observe(database, task)
        elif args.stage == "repair-submit":
            stage_repair_submit(database, task, env)
        elif args.stage == "repair-resume":
            stage_repair_resume(database, task, env)
        elif args.stage == "publish-submit":
            stage_publish_submit(database, task, env)
        elif args.stage == "publish-resume":
            stage_publish_resume(database, task, env)
        elif args.stage == "body-sync":
            stage_body_sync(database, task, env)
        else:
            stage_evidence(database, task)
    finally:
        subprocess.run(["pg_ctl", "-D", str(ROOT / "db"), "-m", "fast", "-w", "stop"],
                       env=env, capture_output=True, text=True, timeout=40)


if __name__ == "__main__":
    main()
