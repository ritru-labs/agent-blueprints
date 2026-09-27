"""Verify a downloaded candidate in a separate, constrained Docker container."""

import argparse
import datetime
import json
import pathlib
import re
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

from archive_intake import baseline_files, materialize_candidate, read_candidate
from config import BASE_COMMIT, IMAGE_ID

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
SPIKE = HERE.parent / "agents-api-spike"
DEFAULT_ARTIFACT = SPIKE / ".spike-runs/sample-project.zip"
EVIDENCE = SPIKE / "evidence/phase-1a-live-run.json"
OUTPUT = HERE / ".verifier-runs/last-result.json"
MAX_OUTPUT = 2_000


def pinned_image_available():
    result = subprocess.run(
        ["docker", "image", "inspect", IMAGE_ID, "--format", "{{.Id}}"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode or result.stdout.strip() != IMAGE_ID:
        raise RuntimeError("pinned local Python verifier image is unavailable")


def container_command(candidate_dir, check_name):
    if check_name not in ("trusted_requirement", "candidate_tests"):
        raise ValueError("unknown verifier check")
    name = f"phase1b-{uuid.uuid4().hex[:12]}"
    test_dir = "/checks" if check_name == "trusted_requirement" else "/workspace/sample/tests"
    args = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "256m", "--cpus", "1", "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--mount", f"type=bind,source={candidate_dir},target=/workspace,readonly",
        "--mount", f"type=bind,source={HERE / 'trusted_tests'},target=/checks,readonly",
        "--workdir", "/workspace", "--env", "PYTHONPATH=/workspace",
        "--env", "PYTHONDONTWRITEBYTECODE=1", IMAGE_ID,
        "python3", "-B", "-m", "unittest", "discover", "-s", test_dir,
        "-p", "test_*.py", "-v",
    ]
    return name, args


def run_check(candidate_dir, check_name):
    name, command = container_command(candidate_dir, check_name)
    head = bytearray()
    tail = bytearray()
    total_output = [0]
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def drain():
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            total_output[0] += len(chunk)
            if len(head) < MAX_OUTPUT:
                head.extend(chunk[: MAX_OUTPUT - len(head)])
            tail.extend(chunk)
            if len(tail) > MAX_OUTPUT // 2:
                del tail[: len(tail) - MAX_OUTPUT // 2]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
        exit_code = None
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10, check=False)
    reader.join(timeout=2)
    if total_output[0] <= MAX_OUTPUT:
        output = head.decode("utf-8", "replace")
    else:
        output = head[: MAX_OUTPUT // 2].decode("utf-8", "replace") + "\n...[truncated]...\n" + tail.decode("utf-8", "replace")
    match = re.search(r"Ran (\d+) tests?", output)
    test_count = int(match.group(1)) if match else None
    ok = bool(re.search(r"^OK$", output, re.MULTILINE))
    failed_marker = bool(re.search(r"^FAILED", output, re.MULTILINE))
    summary = f"Ran {match.group(1)} {'test' if match.group(1) == '1' else 'tests'}" if match else "No test count observed"
    if ok:
        summary += "; OK"
    elif failed_marker:
        summary += "; FAILED"
    if timed_out:
        summary += "; timed out"
    return {
        "name": check_name,
        "command": f"python3 -B -m unittest discover -s {('/checks' if check_name == 'trusted_requirement' else '/workspace/sample/tests')} -p test_*.py -v",
        "exit_code": exit_code,
        "test_count": test_count,
        "ok": ok,
        "failed_marker": failed_marker,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "summary": summary,
        "output_excerpt": output,
    }


def check_passed(result, minimum_tests, exact_tests=None):
    count = result.get("test_count")
    return (result.get("exit_code") == 0 and isinstance(count, int) and
            count >= minimum_tests and (exact_tests is None or count == exact_tests) and
            result.get("ok") is True and result.get("timed_out") is False)


def verify(artifact_path, expected_sha256, base_commit=BASE_COMMIT):
    digest, accepted = read_candidate(artifact_path, expected_sha256)
    baseline = baseline_files(REPO_ROOT, base_commit)
    pinned_image_available()
    with tempfile.TemporaryDirectory(prefix="jira-pr-phase1b-", dir="/private/tmp") as temporary:
        candidate_dir = pathlib.Path(temporary)
        tree_hash, manifest = materialize_candidate(candidate_dir, baseline, accepted)
        checks = [run_check(candidate_dir, "trusted_requirement")]
        trusted_ok = check_passed(checks[0], minimum_tests=5, exact_tests=5)
        if trusted_ok:
            checks.append(run_check(candidate_dir, "candidate_tests"))
    candidate_ok = len(checks) == 2 and check_passed(checks[1], minimum_tests=1)
    return {
        "artifact_sha256": digest,
        "base_commit": base_commit,
        "candidate_tree_sha256": tree_hash,
        "file_sha256": manifest,
        "verifier_image_id": IMAGE_ID,
        "isolation": {
            "network": "none", "root_filesystem": "read-only",
            "candidate_mount": "read-only", "trusted_tests_mount": "read-only",
            "user": "65534:65534", "capabilities": "ALL dropped",
            "no_new_privileges": True, "memory": "256m", "pids_limit": 64,
        },
        "checks": checks,
        "status": "PASS" if trusted_ok and candidate_ok else "FAIL",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=pathlib.Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--source-session-id")
    parser.add_argument("--source-artifact-id")
    parser.add_argument("--base-commit", default=BASE_COMMIT)
    args = parser.parse_args()
    supplied = (args.expected_sha256, args.source_session_id, args.source_artifact_id)
    if any(supplied) and not all(supplied):
        parser.error("expected digest, session ID, and artifact ID must be supplied together")
    trusted = json.loads(EVIDENCE.read_text()) if not all(supplied) else None
    source_session_id = args.source_session_id or trusted["session"]["id"]
    source_artifact_id = args.source_artifact_id or trusted["artifact"]["id"]
    expected_sha256 = args.expected_sha256 or trusted["artifact"]["sha256"]
    result = {
        "schema_version": 1,
        "phase": "1B",
        "verified_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_session_id": source_session_id,
        "source_artifact_id": source_artifact_id,
        "status": "FAIL",
    }
    try:
        result.update(verify(args.artifact, expected_sha256, args.base_commit))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result["error"] = str(error)[:300]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Phase 1B {result['status']}; evidence: {args.output}")
    for check in result.get("checks", []):
        print(f"{check['name']}: exit={check['exit_code']} {check['summary']}")
    if "error" in result:
        print(f"reason: {result['error']}")
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
