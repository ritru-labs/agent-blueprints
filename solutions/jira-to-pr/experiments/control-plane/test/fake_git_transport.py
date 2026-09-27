"""Local bare Git transport with persisted fake GitHub PR readback for crash tests."""

import hashlib
import pathlib

from github_publisher import GitHubPublisher
from trusted_publisher import LocalBarePublisher


class FakeGitTransport(LocalBarePublisher):
    def __init__(self, database, policy, artifact_store, remote, github):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.remote = pathlib.Path(remote).resolve(strict=True)
        self.github = github
        self.remote_kind = "github"
        self.remote_id = hashlib.sha256(str(self.remote).encode()).hexdigest()
        self.config_hash = "c" * 64

    def _preflight(self, run):
        return GitHubPublisher._preflight(self, run)

    def _assert_pr_head(self, run_id, expected_sha):
        return GitHubPublisher._assert_pr_head(self, run_id, expected_sha)

    def _remote_head(self, branch_ref):
        sha = super()._remote_head(branch_ref)
        state = self.github._read()
        if sha is None:
            state["refs"].pop(branch_ref, None)
        else:
            state["refs"][branch_ref] = sha
            state.setdefault("trees", {})[sha] = self._git(
                "--git-dir", str(self.remote), "rev-parse", f"{sha}^{{tree}}")
        for pull in state["pulls"]:
            if pull["head"]["ref"] == branch_ref.removeprefix("refs/heads/"):
                pull["head"]["sha"] = sha
        self.github._write(state)
        return sha
