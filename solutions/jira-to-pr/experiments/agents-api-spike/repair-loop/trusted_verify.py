"""Return only trusted, allowlisted verifier findings to the coordinator."""

import argparse
import json
import pathlib
import re
import sys

VERIFIER = pathlib.Path(__file__).resolve().parents[2] / "trusted-verifier"
sys.path.insert(0, str(VERIFIER))
from verify_candidate import verify  # noqa: E402

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ADD_TEST = "test_add_handles_positive_zero_and_negative_values"
KNOWN_TESTS = {ADD_TEST, "test_existing_identity_behavior",
               "test_application_secrets_are_absent", "test_candidate_mount_is_read_only",
               "test_outbound_network_is_unavailable"}


def sanitize(result):
    checks = []
    findings = []
    for check in result["checks"]:
        name = check["name"]
        if name not in ("trusted_requirement", "candidate_tests"):
            raise ValueError("unknown check name")
        checks.append({key: check.get(key) for key in
                       ("name", "exit_code", "test_count", "ok", "timed_out")})
        if check.get("ok") is True and check.get("exit_code") == 0:
            continue
        if name == "candidate_tests":
            findings.append({"code": "CANDIDATE_TESTS_FAILED",
                             "message": "Candidate tests failed; inspect and repair them."})
            continue
        output = check.get("output_excerpt", "")
        failed = set(re.findall(r"^(?:FAIL|ERROR): (test_[a-z_]+) ", output, re.MULTILINE))
        if not failed or not failed <= KNOWN_TESTS:
            findings.append({"code": "TRUSTED_VERIFIER_UNCLASSIFIED",
                             "message": "Trusted verification failed without a safe repair finding."})
        elif failed == {ADD_TEST}:
            findings.append({"code": "ADD_ARITHMETIC",
                             "message": "add(a, b) must return the arithmetic sum for positive, zero, and negative integers."})
        else:
            findings.append({"code": "TRUSTED_BOUNDARY_FAILURE",
                             "message": "A trusted isolation or existing-behavior check failed."})
    return {"status": result["status"], "artifact_sha256": result["artifact_sha256"],
            "candidate_tree_sha256": result["candidate_tree_sha256"],
            "base_commit": result["base_commit"],
            "verifier_image_id": result["verifier_image_id"],
            "checks": checks, "findings": findings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=pathlib.Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if not SHA256.fullmatch(args.expected_sha256):
        raise ValueError("invalid trusted digest")
    print(json.dumps(sanitize(verify(args.artifact, args.expected_sha256)), sort_keys=True))


if __name__ == "__main__":
    main()
