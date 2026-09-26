"""A verifier-shaped PASS cannot bypass the controller's exact-candidate gate."""

import json
import pathlib
import unittest

from controller import Controller
from policy import Policy

HERE = pathlib.Path(__file__).resolve().parents[1]


class ResultGateTests(unittest.TestCase):
    def setUp(self):
        policy = Policy.load(HERE / "policy.json")
        self.controller = Controller(None, policy, None, None)
        phase1b = json.loads((HERE.parent / "trusted-verifier/evidence/phase-1b-verification.json").read_text())
        self.run = {"session_id": phase1b["source"]["session_id"]}
        self.candidate = {
            "source_session_id": self.run["session_id"],
            "source_artifact_id": phase1b["source"]["artifact_id"],
            "archive_sha256": phase1b["source"]["artifact_sha256"],
            "tree_sha256": phase1b["candidate_tree_sha256"],
            "base_commit": phase1b["base_commit"],
        }
        self.result = {
            "status": "PASS", "source_session_id": self.run["session_id"],
            "source_artifact_id": self.candidate["source_artifact_id"],
            "artifact_sha256": self.candidate["archive_sha256"],
            "candidate_tree_sha256": self.candidate["tree_sha256"],
            "base_commit": self.candidate["base_commit"],
            "verifier_image_id": policy.document["verifier_image_id"],
            "isolation": {"network": "none"},
            "checks": [
                {"name": "trusted_requirement", "exit_code": 0, "test_count": 5,
                 "ok": True, "timed_out": False, "summary": "Ran 5 tests; OK"},
                {"name": "candidate_tests", "exit_code": 0, "test_count": 2,
                 "ok": True, "timed_out": False, "summary": "Ran 2 tests; OK"},
            ],
        }

    def test_rejects_success_with_zero_trusted_tests(self):
        self.result["checks"][0]["test_count"] = 0
        with self.assertRaisesRegex(ValueError, "lacks required test evidence"):
            self.controller._sanitized_verification(self.result, self.run, self.candidate)

    def test_rejects_result_for_a_different_candidate_tree(self):
        self.result["candidate_tree_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match the stored candidate"):
            self.controller._sanitized_verification(self.result, self.run, self.candidate)


if __name__ == "__main__":
    unittest.main()
