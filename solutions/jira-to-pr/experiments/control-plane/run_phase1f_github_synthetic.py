"""Real GitHub CI/PR qualification with a labelled synthetic local repair source.

This keeps the GitHub and PostgreSQL proof moving when the Agents API account
cannot create sessions. It is not the final same-session agent repair proof.
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import zipfile

from adapters import ContentAddressedStore, Phase1BTrustedVerifier, SavedTurnArtifact
from controller import Controller
from database import Store
from policy import Policy
import run_phase1f_qualification as live
from run_github_draft import start_pg

HERE = pathlib.Path(__file__).resolve().parent
SPIKE = HERE.parent / "agents-api-spike/sample-project"
live.ROOT = HERE / ".control-runs/phase1f-github-synthetic"
ROOT = live.ROOT


def candidate_zip(name, marker):
    path = ROOT / name
    app = (SPIKE / "sample/app.py").read_text() + "\n\ndef add(a, b):\n    return a + b\n"
    if marker:
        app += "\n# PHASE1F_REPAIR_REQUIRED\n"
    tests = ((SPIKE / "sample/tests/test_app.py").read_text() +
             "\nfrom sample.app import add\n\nclass AdditionTests(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(2, -3), -1)\n")
    if not marker:
        tests += "    def test_negative(self):\n        self.assertEqual(add(-4, 6), 2)\n"
    if not path.exists():
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("sample/__init__.py", (SPIKE / "sample/__init__.py").read_bytes())
            bundle.writestr("sample/app.py", app)
            bundle.writestr("sample/tests/test_app.py", tests)
        path.chmod(0o600)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("initial", "observe", "repair", "publish-submit",
                                          "publish-resume", "body-sync", "evidence"))
    parser.add_argument("--execute-github-writes", action="store_true")
    args = parser.parse_args()
    if args.stage in ("initial", "publish-submit", "publish-resume", "body-sync") and not args.execute_github_writes:
        parser.error("explicit --execute-github-writes is required for branch/PR stages")
    if args.stage != "initial" and not (ROOT / "run.json").exists():
        raise RuntimeError("synthetic GitHub qualification run has not been initialized")
    state = live.initialize() if args.stage == "initial" else json.loads((ROOT / "run.json").read_text())
    env = live.safe_environment(None)
    dsn = start_pg(ROOT, env)
    env = live.safe_environment(dsn)
    try:
        database = Store(dsn)
        database.migrate()
        task = state["task_key"]
        if args.stage == "initial":
            try:
                run = database.get_run(task)
            except KeyError:
                archive, digest = candidate_zip("synthetic-first.zip", True)
                controller = Controller(database, Policy.load(ROOT / "policy.json"),
                                        ContentAddressedStore(ROOT / "artifacts"),
                                        Phase1BTrustedVerifier())
                controller.admit_candidate(task, SavedTurnArtifact(
                    f"synthetic_local_{task}", "synthetic_turn_1", "synthetic_artifact_1",
                    digest, archive))
                verified = controller.resume(task)
                if verified["state"] != "VERIFIED":
                    raise RuntimeError("first synthetic artifact failed independent Docker verification")
                run = database.get_run(task)
            if run["state"] != "VERIFIED":
                raise RuntimeError("synthetic candidate is not verified")
            live.check_child(live.github_child("publish", task, env), "synthetic first publication")
            live.check_child(live.github_child("draft-pr", task, env), "synthetic draft PR")
            result = database.summary(task)
            print(json.dumps({"stage": "initial", "scope": "synthetic_local_candidate",
                              "pr_url": result["draft_pr_url"],
                              "head_commit_sha": result["published_commit_sha"]}, sort_keys=True))
        elif args.stage == "observe":
            live.stage_observe(database, task)
        elif args.stage == "repair":
            run = database.get_run(task)
            observation = database.latest_pr_observation(run["id"])
            if observation is None or observation["gate"] != "FAIL":
                raise RuntimeError("exact-head CI failure has not been observed")
            controller = Controller(database, Policy.load(ROOT / "policy.json"),
                                    ContentAddressedStore(ROOT / "artifacts"),
                                    Phase1BTrustedVerifier())
            if database.current_ci_repair(run["id"]) is None:
                intent = controller.plan_ci_repair(task)
                controller.mark_ci_repair_uncertain(task)
                controller.observe_ci_repair(task, run["session_id"], intent["input_sha256"],
                                             "synthetic_message_1", "synthetic_turn_2")
            if database.get_run(task)["state"] == "VERIFIED" and \
                    database.get_candidate(database.get_run(task))["ordinal"] == 1:
                archive, digest = candidate_zip("synthetic-second.zip", False)
                controller.admit_candidate(task, SavedTurnArtifact(
                    run["session_id"], "synthetic_turn_2", "synthetic_artifact_2",
                    digest, archive))
            verified = controller.resume(task)
            if verified["state"] != "VERIFIED" or verified["candidate_count"] != 2:
                raise RuntimeError("second synthetic artifact failed fresh Docker verification")
            print(json.dumps({"stage": "repair", "scope": "synthetic_local_repair_source",
                              "candidate_count": 2, "verification_count": 2}, sort_keys=True))
        elif args.stage == "publish-submit":
            live.stage_publish_submit(database, task, env)
        elif args.stage == "publish-resume":
            live.stage_publish_resume(database, task, env)
        elif args.stage == "body-sync":
            live.stage_body_sync(database, task, env)
        else:
            run = database.get_run(task)
            with database.connect() as conn:
                candidates = conn.execute("SELECT * FROM candidate_artifacts WHERE run_id = %s "
                                          "ORDER BY ordinal", (run["id"],)).fetchall()
                verifications = conn.execute("SELECT * FROM verification_runs WHERE run_id = %s "
                                             "ORDER BY created_at, id", (run["id"],)).fetchall()
                publications = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s "
                                            "ORDER BY created_at, id", (run["id"],)).fetchall()
                heads = conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s ORDER BY ordinal",
                                     (run["id"],)).fetchall()
                batches = conn.execute("SELECT head_commit_sha, gate FROM pr_observation_batches "
                                       "WHERE run_id = %s ORDER BY observed_at, id",
                                       (run["id"],)).fetchall()
                checks = conn.execute(
                    "SELECT b.head_commit_sha, c.check_name, c.app_slug, c.check_id, "
                    "c.run_attempt, c.status, c.conclusion, c.started_at, c.completed_at "
                    "FROM pr_check_observations c JOIN pr_observation_batches b "
                    "ON b.id = c.batch_id WHERE b.run_id = %s ORDER BY b.observed_at, c.check_id",
                    (run["id"],)).fetchall()
                counts = conn.execute(
                    "SELECT (SELECT count(*) FROM candidate_artifacts WHERE run_id = %s) AS candidates, "
                    "(SELECT count(*) FROM verification_runs WHERE run_id = %s) AS verifications, "
                    "(SELECT count(*) FROM publication_attempts WHERE run_id = %s) AS publications, "
                    "(SELECT count(*) FROM draft_pr_attempts WHERE run_id = %s) AS draft_prs, "
                    "(SELECT count(*) FROM ci_repair_intents WHERE run_id = %s) AS ci_repairs",
                    (run["id"],) * 5).fetchone()
            if (len(heads), counts["candidates"], counts["verifications"],
                    counts["publications"], counts["draft_prs"], counts["ci_repairs"]) != (
                    2, 2, 2, 2, 1, 1) or not any(
                    row["head_commit_sha"] == heads[0]["head_commit_sha"] and row["gate"] == "FAIL"
                    for row in batches) or not any(
                    row["head_commit_sha"] == heads[1]["head_commit_sha"] and row["gate"] == "PASS"
                    for row in batches):
                raise RuntimeError("synthetic GitHub CI qualification evidence is incomplete")
            if (len({c["archive_sha256"] for c in candidates}) != 2 or
                    len({c["tree_sha256"] for c in candidates}) != 2 or
                    any(c["id"] != v["candidate_id"] or c["id"] != p["candidate_id"] or
                        v["id"] != p["verification_id"] or h["publication_id"] != p["id"]
                        for c, v, p, h in zip(candidates, verifications, publications, heads))):
                raise RuntimeError("synthetic GitHub candidate and verifier linkage differs")
            draft = database.get_draft_pr(run["id"])
            body_update = database.get_pr_body_update(heads[-1]["id"])
            if body_update is None or body_update["state"] != "CONFIRMED":
                raise RuntimeError("synthetic GitHub PR body does not name the current candidate")
            evidence = {"phase": "1F", "scope": "real GitHub CI and PR; synthetic local candidate and repair",
                        "status": "PARTIAL", "agent_session_continuation": False,
                        "run_id": str(run["id"]), "draft_pr_url": draft["pr_url"],
                        "candidate_ids": [str(c["id"]) for c in candidates],
                        "candidate_artifact_sha256": [c["archive_sha256"] for c in candidates],
                        "candidate_tree_sha256": [c["tree_sha256"] for c in candidates],
                        "verification_ids": [str(v["id"]) for v in verifications],
                        "verification_statuses": [v["status"] for v in verifications],
                        "publication_ids": [str(p["id"]) for p in publications],
                        "pr_body_update_id": str(body_update["id"]),
                        "pr_body_update_state": body_update["state"],
                        "head_commit_sha": [h["head_commit_sha"] for h in heads],
                        "gates": [{"head_commit_sha": x["head_commit_sha"], "gate": x["gate"]}
                                  for x in batches],
                        "required_checks": [
                            {key: value.isoformat() if hasattr(value, "isoformat") else value
                             for key, value in row.items()} for row in checks],
                        "counts": counts, "branch_update_exit_code": live.progress().get("branch_update_exit_code"),
                        "postgres_restarted_between_stages": True,
                        "automatic_merge": False}
            live.save_private(ROOT / "phase1f-synthetic-github-result.json", evidence)
            print(json.dumps({"stage": "evidence", "status": "PARTIAL",
                              "pr_url": draft["pr_url"]}, sort_keys=True))
    finally:
        subprocess.run(["pg_ctl", "-D", str(ROOT / "db"), "-m", "fast", "-w", "stop"],
                       env=env, capture_output=True, text=True, timeout=40)


if __name__ == "__main__":
    main()
