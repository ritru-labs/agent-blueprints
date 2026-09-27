"""Version 2 GitHub publication must be an exact trusted opt-in."""

import json
import pathlib
import tempfile
import unittest

from policy import Policy

HERE = pathlib.Path(__file__).resolve().parents[1]


class GitHubPolicyTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((HERE / "policy.json").read_text())
        self.document.update({"schema_version": 2, "external_writes_enabled": True,
                              "allowed_operation_kinds": ["git_branch", "draft_pr"],
                              "github_target_base": "refs/heads/qualification-base",
                              "github_actor_login": "binnukyadari"})

    def load(self, document):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "policy.json"
            path.write_text(json.dumps(document))
            return Policy.load(path)

    def test_exact_github_policy_is_accepted(self):
        self.assertEqual(self.load(self.document).document, self.document)
        dotted = dict(self.document, github_target_base="refs/heads/feat/jira-to-pr-v0.1")
        self.assertEqual(self.load(dotted).document, dotted)

    def test_actor_branch_extra_operations_and_write_switch_are_rejected(self):
        changes = (
            {"github_actor_login": "another-user"},
            {"github_target_base": "refs/heads/../main"},
            {"github_target_base": "refs/heads/main//other"},
            {"allowed_operation_kinds": ["git_branch", "draft_pr", "jira_write"]},
            {"external_writes_enabled": False},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(dict(self.document, **change))


if __name__ == "__main__":
    unittest.main()
