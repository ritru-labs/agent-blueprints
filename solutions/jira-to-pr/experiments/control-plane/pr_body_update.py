"""Durable readback-checked body refresh on the existing draft PR."""

import hashlib
import os

from adapters import reconstruct_tree_hash
from draft_pr import DraftPRCoordinator
from github_api import REPOSITORY


class PRBodyConflict(RuntimeError):
    pass


class DraftPRBodyCoordinator:
    def __init__(self, database, policy, artifact_store, github_api):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.github = github_api

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def _remote_pull(self, run, draft, publication):
        pull = self.github.pull(draft["pr_number"])
        if not isinstance(pull, dict):
            raise PRBodyConflict("existing draft PR could not be read")
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        if (pull.get("number") != draft["pr_number"] or pull.get("state") != "open" or
                pull.get("draft") is not True or
                (pull.get("user") or {}).get("login") != draft["actor_login"] or
                head.get("sha") != publication["commit_sha"] or
                head.get("ref") != draft["head_ref"].removeprefix("refs/heads/") or
                (head.get("repo") or {}).get("full_name") != REPOSITORY or
                base.get("ref") != draft["base_ref"].removeprefix("refs/heads/") or
                base.get("sha") != run["base_commit"] or
                (base.get("repo") or {}).get("full_name") != REPOSITORY or
                self.github.ref_sha(draft["head_ref"]) != publication["commit_sha"] or
                self.github.ref_sha(draft["base_ref"]) != run["base_commit"] or
                self.github.commit_tree_sha(publication["commit_sha"]) != publication["git_tree_sha"] or
                self._digest(pull.get("title") or "") != draft["title_sha256"]):
            raise PRBodyConflict("draft PR, base, head, or Git tree differs from durable identity")
        return pull

    def sync(self, task_key, *, interrupt_after_update=False):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            candidate = self.database.get_candidate(run)
            verification = self.database.get_verification(run)
            publication = self.database.get_publication(run["id"])
            draft = self.database.get_draft_pr(run["id"])
            head = self.database.latest_pr_head(run["id"])
            if (run["state"] != "VERIFIED" or candidate is None or verification is None or
                    verification["status"] != "PASS" or publication is None or
                    publication["state"] != "CONFIRMED" or draft is None or
                    draft["state"] != "CONFIRMED" or head is None or head["ordinal"] < 2 or
                    head["candidate_id"] != candidate["id"] or
                    head["verification_id"] != verification["id"] or
                    head["publication_id"] != publication["id"]):
                raise PRBodyConflict("current verified PR head is absent")
            archive = self.artifact_store.checked_path(candidate["archive_sha256"],
                                                       candidate["storage_path"])
            if reconstruct_tree_hash(archive, candidate["archive_sha256"],
                                     candidate["base_commit"]) != candidate["tree_sha256"]:
                raise PRBodyConflict("current candidate artifact changed before PR body update")
            _, body = DraftPRCoordinator._content(run, candidate, verification, publication)
            previous_hash = self.database.prior_pr_body_sha(run["id"], head)
            if previous_hash is None:
                raise PRBodyConflict("previous PR body acknowledgement is absent")
            body_hash = self._digest(body)
            intent = {"draft_pr_id": draft["id"], "head_link_id": head["id"],
                      "candidate_id": candidate["id"], "verification_id": verification["id"],
                      "publication_id": publication["id"],
                      "head_commit_sha": publication["commit_sha"],
                      "previous_body_sha256": previous_hash, "body_sha256": body_hash,
                      "operation_key": hashlib.sha256(
                          f"{run['id']}:{head['id']}:{body_hash}:pr-body".encode()).hexdigest()}
            row = self.database.plan_pr_body_update(run["id"], intent)
            pull = self._remote_pull(run, draft, publication)
            actual_hash = self._digest(pull.get("body") or "")
            if actual_hash == body_hash:
                if row["state"] == "PLANNED":
                    raise PRBodyConflict("remote body changed before uncertain local submission")
                self.database.confirm_pr_body_update(head["id"])
                return self.database.summary(task_key)
            if actual_hash != previous_hash:
                raise PRBodyConflict("draft PR body differs from the expected previous content")
            if row["state"] != "PLANNED":
                raise PRBodyConflict("uncertain PR body write has no remote match; do not retry blindly")
            self.database.mark_pr_body_update_unknown(head["id"])
            self.github.update_draft_pull_body(draft["pr_number"], body)
            if interrupt_after_update:
                os._exit(75)
            pull = self._remote_pull(run, draft, publication)
            if self._digest(pull.get("body") or "") != body_hash:
                raise PRBodyConflict("GitHub did not read back the exact updated PR body")
            self.database.confirm_pr_body_update(head["id"])
            return self.database.summary(task_key)
