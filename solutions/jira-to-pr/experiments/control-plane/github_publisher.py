"""Scoped GitHub transport for the exact-candidate trusted Git publisher."""

import hashlib
import json
import pathlib
import tempfile

from github_api import REPOSITORY
from trusted_publisher import LocalBarePublisher, PublicationError


class GitHubPublisher(LocalBarePublisher):
    REMOTE = f"https://github.com/{REPOSITORY}.git"

    def __init__(self, database, policy, artifact_store, github_api):
        if (policy.document["schema_version"] != 2 or
                policy.document["external_writes_enabled"] is not True or
                "git_branch" not in policy.document["allowed_operation_kinds"]):
            raise ValueError("GitHub publication needs the explicit version 2 write policy")
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.github = github_api
        self.remote = self.REMOTE
        self.remote_kind = "github"
        self.remote_id = hashlib.sha256(self.REMOTE.encode()).hexdigest()
        config = {
            "schema_version": 2,
            "repository": REPOSITORY,
            "base_commit": policy.document["base_commit"],
            "target_base_ref": policy.document["github_target_base"],
            "actor_login": policy.document["github_actor_login"],
            "remote_id": self.remote_id, "remote_kind": "github",
            "branch_namespace": "refs/heads/agent/",
            "author": "Ritru Agent Publisher <agent-publisher@users.noreply.github.com>",
        }
        self.config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _git(self, *args, **kwargs):
        with tempfile.TemporaryDirectory(prefix="phase1e-git-credential-") as temporary:
            helper = pathlib.Path(temporary) / "askpass.sh"
            helper.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf 'x-access-token\\n' ;;\n"
                "  *) printf '%s\\n' \"$JIRA_TO_PR_GITHUB_TOKEN\" ;;\n"
                "esac\n"
            )
            helper.chmod(0o700)
            extra = dict(kwargs.pop("env_extra", {}) or {})
            extra.update({
                "GIT_ALLOW_PROTOCOL": "https", "GIT_ASKPASS": str(helper),
                "JIRA_TO_PR_GITHUB_TOKEN": self.github.credential_for_trusted_git(),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
            })
            return super()._git(*args, env_extra=extra, **kwargs)

    def _preflight(self, run):
        document = self.policy.document
        if self.github.actor_login() != document["github_actor_login"]:
            raise PublicationError("active GitHub account differs from the pinned publisher actor")
        repository = self.github.repository()
        if (not repository or repository.get("full_name") != REPOSITORY or
                repository.get("permissions", {}).get("push") is not True):
            raise PublicationError("GitHub account lacks the pinned repository write scope")
        if self.github.ref_sha(document["github_target_base"]) != run["base_commit"]:
            raise PublicationError("target base branch moved from the verified base commit")

    def _remote_head(self, branch_ref):
        return self.github.ref_sha(branch_ref)

    def _check_remote_commit(self, intent):
        if self.github.ref_sha(intent["branch_ref"]) != intent["commit_sha"]:
            raise PublicationError("GitHub branch differs from the immutable publication intent")
        if self.github.commit_tree_sha(intent["commit_sha"]) != intent["git_tree_sha"]:
            raise PublicationError("GitHub commit tree differs from the verified publication tree")

    def _assert_pr_head(self, run_id, expected_sha):
        draft = self.database.get_draft_pr(run_id)
        if draft is None or draft["state"] != "CONFIRMED":
            raise PublicationError("existing confirmed draft PR is absent")
        pull = self.github.pull(draft["pr_number"])
        if not isinstance(pull, dict):
            raise PublicationError("existing draft PR could not be read")
        head = (pull or {}).get("head") or {}
        base = (pull or {}).get("base") or {}
        if (pull.get("number") != draft["pr_number"] or pull.get("state") != "open" or
                pull.get("draft") is not True or
                (pull.get("user") or {}).get("login") != draft["actor_login"] or
                head.get("sha") != expected_sha or
                head.get("ref") != draft["head_ref"].removeprefix("refs/heads/") or
                (head.get("repo") or {}).get("full_name") != REPOSITORY or
                base.get("ref") != draft["base_ref"].removeprefix("refs/heads/") or
                (base.get("repo") or {}).get("full_name") != REPOSITORY):
            raise PublicationError("existing draft PR differs from the expected exact head")
