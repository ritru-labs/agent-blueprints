"""Trusted draft-PR intent, exact-head gate, and uncertain-write reconciliation."""

import hashlib
import os

from adapters import reconstruct_tree_hash
from github_api import REPOSITORY


class DraftPRConflict(RuntimeError):
    pass


class DraftPRCoordinator:
    def __init__(self, database, policy, artifact_store, github_api):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.github = github_api

    @staticmethod
    def _content(run, candidate, verification, publication):
        title = f"Draft: verified synthetic candidate {str(run['id'])[:8]}"
        body = (
            "Synthetic agent candidate for human review.\n\n"
            f"Run: `{run['id']}`\n"
            f"Candidate: `{candidate['id']}`\n"
            f"Independent verification: `{verification['id']}` (`PASS`)\n"
            f"Artifact SHA-256: `{candidate['archive_sha256']}`\n"
            f"Verified candidate tree SHA-256: `{candidate['tree_sha256']}`\n"
            f"Published commit: `{publication['commit_sha']}`\n\n"
            "The human reviewer owns merge and deployment.\n"
        )
        return title, body

    def _read_matches(self, row, title, body):
        pulls = self.github.list_pulls(row["base_ref"], row["head_ref"])
        exact = []
        for pull in pulls:
            head = pull.get("head") or {}
            base = pull.get("base") or {}
            if (head.get("ref") != row["head_ref"].removeprefix("refs/heads/") or
                    base.get("ref") != row["base_ref"].removeprefix("refs/heads/") or
                    (head.get("repo") or {}).get("full_name") != REPOSITORY or
                    (base.get("repo") or {}).get("full_name") != REPOSITORY):
                raise DraftPRConflict("GitHub returned a PR with conflicting branch scope")
            if (pull.get("state") != "open" or pull.get("draft") is not True or
                    head.get("sha") != row["head_commit_sha"] or
                    (pull.get("user") or {}).get("login") != row["actor_login"] or
                    hashlib.sha256((pull.get("title") or "").encode()).hexdigest() !=
                    row["title_sha256"] or
                    hashlib.sha256((pull.get("body") or "").encode()).hexdigest() !=
                    row["body_sha256"] or
                    pull.get("title") != title or pull.get("body") != body):
                raise DraftPRConflict("GitHub PR differs from immutable draft intent")
            number = pull.get("number")
            url = pull.get("html_url")
            if type(number) is not int or number <= 0 or \
                    url != f"https://github.com/{REPOSITORY}/pull/{number}":
                raise DraftPRConflict("GitHub PR identity is malformed")
            exact.append((number, url))
        if len(exact) > 1:
            raise DraftPRConflict("more than one PR exists for the publication branch")
        return exact[0] if exact else None

    def create_or_reconcile(self, task_key, *, interrupt_after_create=False):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if (self.policy.document["schema_version"] != 2 or
                    run["state"] != "VERIFIED"):
                raise DraftPRConflict("GitHub draft PR requires an enabled verified run")
            candidate = self.database.get_candidate(run)
            verification = self.database.get_verification(run)
            publication = self.database.get_publication(run["id"])
            if (candidate is None or verification is None or publication is None or
                    verification["status"] != "PASS" or
                    publication["state"] != "CONFIRMED" or
                    publication["remote_kind"] != "github" or
                    publication["candidate_id"] != candidate["id"] or
                    publication["verification_id"] != verification["id"]):
                raise DraftPRConflict("no exact confirmed GitHub publication for current PASS")
            try:
                archive = self.artifact_store.checked_path(candidate["archive_sha256"],
                                                           candidate["storage_path"])
                if reconstruct_tree_hash(archive, candidate["archive_sha256"],
                                         candidate["base_commit"]) != candidate["tree_sha256"]:
                    raise ValueError("candidate tree changed after verification")
            except (OSError, ValueError) as error:
                self.database.needs_human(run["id"], f"draft PR artifact integrity failure: {error}")
                raise DraftPRConflict("candidate integrity failed before draft PR") from error
            document = self.policy.document
            if self.github.actor_login() != document["github_actor_login"]:
                raise DraftPRConflict("authenticated GitHub account differs from policy")
            repository = self.github.repository()
            if not repository or repository.get("full_name") != REPOSITORY or \
                    repository.get("permissions", {}).get("push") is not True:
                raise DraftPRConflict("authenticated account lacks the pinned repository scope")
            base_ref = document["github_target_base"]
            if self.github.ref_sha(base_ref) != run["base_commit"]:
                raise DraftPRConflict("target base branch moved from the verified base commit")
            if self.github.ref_sha(publication["branch_ref"]) != publication["commit_sha"]:
                raise DraftPRConflict("published branch moved from the verified commit")
            if self.github.commit_tree_sha(publication["commit_sha"]) != publication["git_tree_sha"]:
                raise DraftPRConflict("published Git tree differs from immutable publication intent")
            title, body = self._content(run, candidate, verification, publication)
            key = hashlib.sha256(
                f"{run['id']}:{publication['id']}:{publication['commit_sha']}:draft".encode()
            ).hexdigest()
            intent = {
                "publication_id": publication["id"], "candidate_id": candidate["id"],
                "verification_id": verification["id"], "repository": REPOSITORY,
                "base_ref": base_ref, "head_ref": publication["branch_ref"],
                "head_commit_sha": publication["commit_sha"],
                "actor_login": document["github_actor_login"],
                "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "operation_key": key,
            }
            row = self.database.plan_draft_pr(run["id"], intent)
            observed = self._read_matches(row, title, body)
            if observed:
                if row["state"] == "PLANNED":
                    raise DraftPRConflict("remote PR exists without an uncertain local submission")
                self.database.confirm_draft_pr(run["id"], *observed)
                return self.database.summary(task_key)
            if row["state"] != "PLANNED":
                raise DraftPRConflict("uncertain PR creation has no saved match; do not retry blindly")
            self.database.mark_draft_pr_unknown(run["id"])
            self.github.create_draft_pull(title=title, body=body,
                                          base_ref=base_ref, head_ref=publication["branch_ref"])
            if interrupt_after_create:
                os._exit(75)
            observed = self._read_matches(row, title, body)
            if observed is None:
                raise DraftPRConflict("GitHub did not return the created PR on readback")
            self.database.confirm_draft_pr(run["id"], *observed)
            return self.database.summary(task_key)
