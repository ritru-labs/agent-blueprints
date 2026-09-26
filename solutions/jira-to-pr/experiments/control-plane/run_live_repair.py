"""Disposable PostgreSQL + two real Agents API processes; export safe Phase 1D evidence."""

import getpass
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import uuid

from database import Store

HERE = pathlib.Path(__file__).resolve().parent
SPIKE = HERE.parent / "agents-api-spike"
OUTPUT = HERE / ".control-runs/phase-1d-live-result.json"


def command(args, *, env, cwd, timeout=300):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)


def safe_stage(candidate, verification):
    return {
        "candidate_id": candidate["id"], "ordinal": candidate["ordinal"],
        "session_id": candidate["source_session_id"],
        "turn_id": candidate["source_turn_id"],
        "artifact_id": candidate["source_artifact_id"],
        "artifact_sha256": candidate["archive_sha256"],
        "tree_sha256": candidate["tree_sha256"],
        "verification_id": verification["id"], "verification_status": verification["status"],
        "verifier_image_id": verification["evidence"]["verifier_image_id"],
        "checks": verification["evidence"]["checks"],
        "findings": verification["evidence"]["findings"],
    }


def main():
    if not (SPIKE / ".env.local").is_file():
        raise RuntimeError("local gitignored Agents API key file is required")
    for name in ("initdb", "pg_ctl", "node", "docker"):
        if shutil.which(name) is None:
            raise RuntimeError(f"{name} is required")
    if not (HERE / ".venv/bin/python").is_file():
        raise RuntimeError("control-plane .venv is required")
    base_env = dict(os.environ)
    base_env.pop("OPENAI_API_KEY", None)
    with tempfile.TemporaryDirectory(prefix="phase1d-live-pg-", dir="/private/tmp") as temporary:
        root = pathlib.Path(temporary)
        data = root / "db"
        socket = root / "socket"
        socket.mkdir(mode=0o700)
        init = command(["initdb", "-D", str(data), "-A", "trust", "--no-instructions"],
                       env=base_env, cwd=HERE, timeout=40)
        if init.returncode:
            raise RuntimeError(f"initdb failed: {init.stderr[-500:]}")
        options = f"-c listen_addresses='' -c unix_socket_directories={socket} -c unix_socket_permissions=0700"
        start_pg = command(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                            "-o", options, "-w", "start"], env=base_env, cwd=HERE, timeout=40)
        if start_pg.returncode:
            raise RuntimeError(f"PostgreSQL start failed: {start_pg.stderr[-500:]}")
        try:
            dsn = f"host={socket} dbname=postgres user={getpass.getuser()}"
            database = Store(dsn)
            database.migrate()
            task = f"PHASE1D-{uuid.uuid4().hex[:12]}"
            live_env = dict(base_env, PHASE1C_DATABASE_URL=dsn,
                            PHASE1D_RUN_DIR=str(root / "downloads"),
                            PHASE1D_STORE_DIR=str(root / "artifacts"))
            invocation = ["node", "--env-file=.env.local", "repair-loop/integrated.mjs"]
            first = command([*invocation, "start", "--task", task], env=live_env, cwd=SPIKE)
            if first.returncode != 75:
                raise RuntimeError(f"live first process failed ({first.returncode}): "
                                   f"{first.stderr[-500:]} {first.stdout[-500:]}")
            interrupted = database.summary(task)
            if (interrupted["state"], interrupted["candidate_count"],
                interrupted["verification_count"], interrupted["repair_count"]) != (
                    "REPAIR_INPUT_UNKNOWN", 1, 1, 1):
                raise RuntimeError("database did not retain exact uncertain repair state")
            second = command([*invocation, "resume", "--task", task],
                             env=live_env, cwd=SPIKE)
            if second.returncode:
                raise RuntimeError(f"fresh process could not reconcile and resume: "
                                   f"{second.stderr[-500:]} {second.stdout[-500:]}")
            history = database.history(task)
            again = command([*invocation, "resume", "--task", task],
                            env=live_env, cwd=SPIKE, timeout=60)
            if again.returncode or database.history(task) != history:
                raise RuntimeError("repeated recovery changed durable history")
            run = history["run"]
            candidates = history["candidates"]
            verifications = history["verifications"]
            repairs = history["repairs"]
            if (run["state"], run["candidate_count"], run["verification_count"],
                run["repair_count"], run["session_count"]) != ("VERIFIED", 2, 2, 1, 1):
                raise RuntimeError("final PostgreSQL state does not meet Phase 1D proof")
            if ([v["status"] for v in verifications] != ["FAIL", "PASS"] or
                [c["ordinal"] for c in candidates] != [1, 2] or
                len({c["archive_sha256"] for c in candidates}) != 2 or
                len({c["tree_sha256"] for c in candidates}) != 2 or
                any(v["candidate_id"] != c["id"] for v, c in zip(verifications, candidates)) or
                repairs[0]["failed_verification_id"] != verifications[0]["id"] or
                candidates[1]["repair_attempt_id"] != repairs[0]["id"] or
                candidates[1]["source_turn_id"] != repairs[0]["result_turn_id"] or
                repairs[0]["status"] != "OBSERVED"):
                raise RuntimeError("candidate, verifier, or repair lineage is inconsistent")
            evidence = {
                "schema_version": 1, "phase": "1D-B", "status": "PASS",
                "task_key": task, "run_id": run["run_id"],
                "primary_session_id": run["session_id"],
                "session_lineage": history["sessions"],
                "policy_sha256": run["policy_sha256"],
                "first": safe_stage(candidates[0], verifications[0]),
                "repair": {key: repairs[0][key] for key in
                           ("id", "ordinal", "failed_candidate_id", "failed_verification_id",
                            "session_id", "input_key", "input_sha256", "status",
                            "message_item_id", "result_turn_id")},
                "repaired": safe_stage(candidates[1], verifications[1]),
                "process_restart": {
                    "intentional_first_exit_code": first.returncode,
                    "state_before_restart": interrupted["state"],
                    "state_after_restart": run["state"],
                    "saved_message_reconciled_before_candidate_2": True,
                    "repeated_resume_changed_history": False,
                },
                "counts": {key: run[key] for key in
                           ("candidate_count", "verification_count", "repair_count", "session_count")},
            }
            OUTPUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            OUTPUT.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            print(f"Phase 1D-B PASS; run={run['run_id']} session={run['session_id']} "
                  f"evidence={OUTPUT}")
        finally:
            command(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                    env=base_env, cwd=HERE, timeout=40)


if __name__ == "__main__":
    main()
