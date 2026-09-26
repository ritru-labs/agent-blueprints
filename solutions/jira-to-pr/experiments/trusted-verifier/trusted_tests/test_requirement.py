"""Verifier-owned checks; this directory is never taken from the candidate ZIP."""

import os
import socket
import unittest

from sample.app import add, identity


class RequirementTests(unittest.TestCase):
    def test_add_handles_positive_zero_and_negative_values(self):
        for a, b, expected in ((2, 3, 5), (0, 0, 0), (-2, -3, -5), (-2, 3, 1)):
            with self.subTest(a=a, b=b):
                self.assertEqual(add(a, b), expected)

    def test_existing_identity_behavior(self):
        self.assertEqual(identity(7), 7)

    def test_application_secrets_are_absent(self):
        for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "JIRA_TOKEN"):
            with self.subTest(name=name):
                self.assertNotIn(name, os.environ)

    def test_candidate_mount_is_read_only(self):
        with self.assertRaises(OSError):
            with open("/workspace/phase1b-write-probe", "w"):
                pass

    def test_outbound_network_is_unavailable(self):
        with socket.socket() as connection:
            connection.settimeout(0.3)
            with self.assertRaises(OSError):
                connection.connect(("1.1.1.1", 80))


if __name__ == "__main__":
    unittest.main()
