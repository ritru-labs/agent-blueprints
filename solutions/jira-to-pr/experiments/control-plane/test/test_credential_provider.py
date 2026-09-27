"""Credential-provider boundary stays independent of publisher behavior."""

import unittest

from github_api import GitHubAPI, GitHubAPIError


class CredentialProviderTests(unittest.TestCase):
    def test_injected_provider_supplies_trusted_transport_only(self):
        class Provider:
            def token(self):
                return "scoped-installation-token"

        api = GitHubAPI(credential_provider=Provider())
        self.assertEqual(api.credential_for_trusted_git(), "scoped-installation-token")
        self.assertNotIn("scoped-installation-token", repr(api))

    def test_conflicting_or_invalid_credentials_are_rejected(self):
        class Provider:
            def token(self):
                return "bad\ntoken"

        with self.assertRaises(ValueError):
            GitHubAPI(token="direct", credential_provider=Provider())
        with self.assertRaises(GitHubAPIError):
            GitHubAPI(credential_provider=Provider())


if __name__ == "__main__":
    unittest.main()
