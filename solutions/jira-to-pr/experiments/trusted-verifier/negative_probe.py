"""Explicit Docker probe: reject tampering and catch a dishonest candidate test."""

import hashlib
import json
import pathlib
import tempfile

from archive_intake import baseline_files, materialize_candidate, read_candidate
from verify_candidate import DEFAULT_ARTIFACT, EVIDENCE, HERE, REPO_ROOT, run_check


def main():
    trusted_record = json.loads(EVIDENCE.read_text())
    expected_sha = trusted_record["artifact"]["sha256"]
    baseline = baseline_files(REPO_ROOT)
    with tempfile.TemporaryDirectory(prefix="phase1b-negative-", dir="/private/tmp") as temporary:
        root = pathlib.Path(temporary)
        tampered = root / "tampered.zip"
        tampered.write_bytes(DEFAULT_ARTIFACT.read_bytes() + b"tampered")
        tampered_sha = hashlib.sha256(tampered.read_bytes()).hexdigest()
        hash_rejected = False
        try:
            read_candidate(tampered, expected_sha)
        except ValueError as error:
            hash_rejected = "SHA-256" in str(error)

        wrong = dict(baseline)
        wrong["sample/app.py"] = baseline["sample/app.py"] + b"\n\ndef add(a, b):\n    return 0\n"
        wrong["sample/tests/test_app.py"] = (
            b"import unittest\nfrom sample.app import add\n"
            b"class FalseConfidence(unittest.TestCase):\n"
            b"    def test_wrong_answer(self):\n"
            b"        self.assertEqual(add(2, 3), 0)\n"
        )
        candidate = root / "candidate"
        tree_hash, _ = materialize_candidate(candidate, baseline, wrong)
        trusted = run_check(candidate, "trusted_requirement")
        candidate_test = run_check(candidate, "candidate_tests")

    passed = (
        hash_rejected and
        trusted["exit_code"] == 1 and trusted["test_count"] == 5 and
        trusted["failed_marker"] and not trusted["timed_out"] and
        candidate_test["exit_code"] == 0 and candidate_test["test_count"] == 1 and
        candidate_test["ok"] and not candidate_test["timed_out"]
    )
    result = {
        "schema_version": 1,
        "status": "PASS" if passed else "FAIL",
        "tampered_artifact_sha256": tampered_sha,
        "tampered_hash_rejected": hash_rejected,
        "wrong_candidate_tree_sha256": tree_hash,
        "trusted_check_exit_code": trusted["exit_code"],
        "trusted_test_count": trusted["test_count"],
        "trusted_check_summary": trusted["summary"],
        "candidate_test_exit_code": candidate_test["exit_code"],
        "candidate_test_count": candidate_test["test_count"],
        "candidate_test_summary": candidate_test["summary"],
    }
    output = HERE / ".verifier-runs/negative-result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Phase 1B negative probe {result['status']}; evidence: {output}")
    print(f"tampered hash rejected={hash_rejected}")
    print(f"wrong implementation: trusted exit={trusted['exit_code']}, candidate test exit={candidate_test['exit_code']}")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
