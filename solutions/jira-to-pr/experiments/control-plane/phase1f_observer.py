"""Trusted exact-head GitHub CI/review observer.

Only bounded, typed metadata crosses into PostgreSQL. GitHub log and review
prose never become coordinator instructions.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass

from github_api import REPOSITORY


class ObservationConflict(RuntimeError):
    pass


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ObservationPolicy:
    document: dict
    sha256: str

    @classmethod
    def from_document(cls, document):
        if (not isinstance(document, dict) or set(document) !=
                {"schema_version", "required_checks", "review_actors", "required_approvals"} or
                type(document["schema_version"]) is not int or
                document["schema_version"] != 1 or
                not isinstance(document["required_checks"], list) or
                not 1 <= len(document["required_checks"]) <= 12 or
                not isinstance(document["review_actors"], list) or
                len(document["review_actors"]) > 12 or
                type(document["required_approvals"]) is not int or
                not 0 <= document["required_approvals"] <= 2):
            raise ValueError("invalid Phase 1F observation policy")
        checks = document["required_checks"]
        if (any(not isinstance(check, dict) or set(check) not in
                ({"name", "app_slug"}, {"name", "app_slug", "repair_code"}) or
                not isinstance(check["name"], str) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./:-]{0,119}", check["name"]) or
                not isinstance(check["app_slug"], str) or
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", check["app_slug"]) or
                ("repair_code" in check and check["repair_code"] != "REMOVE_PHASE1F_MARKER")
                for check in checks) or
                len({(x["name"], x["app_slug"]) for x in checks}) != len(checks) or
                any(not isinstance(actor, str) or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", actor)
                    for actor in document["review_actors"]) or
                len(set(document["review_actors"])) != len(document["review_actors"]) or
                document["required_approvals"] > len(document["review_actors"])):
            raise ValueError("invalid required check or trusted reviewer identity")
        return cls(document, canonical_digest(document))


class TrustedPRObserver:
    def __init__(self, database, run_policy, observation_policy, github_api):
        self.database = database
        self.run_policy = run_policy
        self.observation_policy = observation_policy
        self.github = github_api

    @staticmethod
    def _sha(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None

    def _exact_pull(self, run, draft, head, publication):
        pull = self.github.pull(draft["pr_number"])
        if not isinstance(pull, dict):
            raise ObservationConflict("draft PR could not be read")
        remote_head = pull.get("head") or {}
        remote_base = pull.get("base") or {}
        if (pull.get("number") != draft["pr_number"] or pull.get("state") != "open" or
                pull.get("draft") is not True or
                remote_head.get("sha") != head["head_commit_sha"] or
                (pull.get("user") or {}).get("login") != draft["actor_login"] or
                remote_head.get("ref") != draft["head_ref"].removeprefix("refs/heads/") or
                (remote_head.get("repo") or {}).get("full_name") != REPOSITORY or
                remote_base.get("ref") != draft["base_ref"].removeprefix("refs/heads/") or
                remote_base.get("sha") != run["base_commit"] or
                (remote_base.get("repo") or {}).get("full_name") != REPOSITORY or
                self.github.ref_sha(draft["head_ref"]) != head["head_commit_sha"] or
                self.github.ref_sha(draft["base_ref"]) != run["base_commit"] or
                self.github.commit_tree_sha(head["head_commit_sha"]) != publication["git_tree_sha"]):
            raise ObservationConflict("PR or branch moved from the durable exact head")

    def _checks(self, head_sha, raw):
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ObservationConflict("check-run response is unbounded or malformed")
        selected = []
        findings = []
        states = []
        for required in self.observation_policy.document["required_checks"]:
            matches = [item for item in raw if isinstance(item, dict) and
                       item.get("name") == required["name"] and
                       (item.get("app") or {}).get("slug") == required["app_slug"]]
            if not matches:
                findings.append({"code": "REQUIRED_CHECK_MISSING", "check": required["name"]})
                states.append("PENDING")
                continue
            if any(type(item.get("id")) is not int or item["id"] <= 0 for item in matches):
                raise ObservationConflict("required check has an invalid run identity")
            item = max(matches, key=lambda check: check["id"])
            status = item.get("status")
            conclusion = item.get("conclusion")
            if (item.get("head_sha") != head_sha or
                    status not in ("queued", "in_progress", "completed") or
                    (status == "completed") != (conclusion is not None) or
                    (conclusion is not None and conclusion not in
                     ("success", "failure", "cancelled", "timed_out", "action_required",
                      "neutral", "skipped", "stale"))):
                raise ObservationConflict("required check is stale or has an unknown result")
            suite = item.get("check_suite") or {}
            attempt = item.get("run_attempt")
            if attempt is not None and (type(attempt) is not int or attempt <= 0):
                raise ObservationConflict("required check attempt is invalid")
            selected.append({"check_name": required["name"], "app_slug": required["app_slug"],
                             "check_id": item["id"],
                             "run_attempt": attempt,
                             "check_suite_id": suite.get("id") if type(suite.get("id")) is int else None,
                             "head_commit_sha": head_sha, "status": status,
                             "conclusion": conclusion, "started_at": item.get("started_at"),
                             "completed_at": item.get("completed_at")})
            if status != "completed":
                states.append("PENDING")
                findings.append({"code": "REQUIRED_CHECK_RUNNING", "check": required["name"]})
            elif conclusion != "success":
                states.append("FAIL")
                finding = {"code": "REQUIRED_CHECK_FAILED", "check": required["name"],
                           "conclusion": conclusion}
                if required.get("repair_code") and conclusion == "failure":
                    finding["repair_code"] = required["repair_code"]
                findings.append(finding)
            else:
                states.append("PASS")
        return selected, findings, states

    def _reviews(self, head_sha, raw):
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ObservationConflict("review response is unbounded or malformed")
        selected = []
        findings = []
        states = []
        latest_by_reviewer = {}
        trusted = set(self.observation_policy.document["review_actors"])
        for item in raw:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                raise ObservationConflict("review identity is malformed")
            review_head = item.get("commit_id")
            if not self._sha(review_head):
                raise ObservationConflict("review has no exact commit identity")
            state = item.get("state")
            if state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
                raise ObservationConflict("review state is unsupported")
            reviewer = (item.get("user") or {}).get("login")
            if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", reviewer):
                raise ObservationConflict("reviewer identity is malformed")
            body = item.get("body") or ""
            if not isinstance(body, str):
                raise ObservationConflict("review body is malformed")
            code = None
            if review_head == head_sha and state != "DISMISSED":
                if reviewer not in trusted and (body or state == "CHANGES_REQUESTED"):
                    code = "UNSAFE_REVIEW"
                elif body and body.strip() == "RITRU-REVIEW:ADD_ARITHMETIC" and reviewer in trusted:
                    code = "ADD_ARITHMETIC"
                elif body:
                    code = "UNSAFE_REVIEW"
                if code == "UNSAFE_REVIEW":
                    states.append("NEEDS_HUMAN")
                    findings.append({"code": "UNSAFE_REVIEW", "review_id": item["id"]})
                elif state == "CHANGES_REQUESTED":
                    states.append("FAIL" if code == "ADD_ARITHMETIC" else "NEEDS_HUMAN")
                    findings.append({"code": code or "UNSAFE_REVIEW", "review_id": item["id"]})
            if review_head == head_sha and reviewer in trusted:
                prior = latest_by_reviewer.get(reviewer)
                if prior is None or item["id"] > prior["review_id"]:
                    latest_by_reviewer[reviewer] = {
                        "review_id": item["id"], "state": state, "finding_code": code}
            selected.append({"review_id": item["id"], "reviewer": reviewer,
                             "review_head_sha": review_head, "state": state,
                             "finding_code": code,
                             "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                             "submitted_at": item.get("submitted_at")})
        approvals = {reviewer for reviewer, latest in latest_by_reviewer.items()
                     if latest["state"] == "APPROVED" and latest["finding_code"] is None}
        if len(approvals) < self.observation_policy.document["required_approvals"]:
            states.append("PENDING")
            findings.append({"code": "REQUIRED_APPROVAL_MISSING"})
        return selected, findings, states

    def _comments(self, head_sha, raw):
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ObservationConflict("review-comment response is unbounded or malformed")
        selected = []
        findings = []
        states = []
        trusted = set(self.observation_policy.document["review_actors"])
        for item in raw:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                raise ObservationConflict("review-comment identity is malformed")
            review_head = item.get("commit_id")
            original_head = item.get("original_commit_id")
            if not self._sha(review_head) or not self._sha(original_head):
                raise ObservationConflict("review comment has no exact commit identity")
            reviewer = (item.get("user") or {}).get("login")
            if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", reviewer):
                raise ObservationConflict("review-comment actor is malformed")
            body = item.get("body")
            if not isinstance(body, str):
                raise ObservationConflict("review-comment body is malformed")
            code = None
            if review_head == head_sha:
                if (reviewer in trusted and original_head == head_sha and
                        body.strip() == "RITRU-REVIEW:ADD_ARITHMETIC"):
                    code = "ADD_ARITHMETIC"
                    states.append("FAIL")
                else:
                    code = "UNSAFE_REVIEW"
                    states.append("NEEDS_HUMAN")
                findings.append({"code": code, "comment_id": item["id"]})
            selected.append({"comment_id": item["id"], "reviewer": reviewer,
                             "review_head_sha": review_head, "finding_code": code,
                             "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                             "created_at": item.get("created_at")})
        return selected, findings, states

    def observe(self, task_key, *, interrupt_after_read=False):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.run_policy)
            draft = self.database.get_draft_pr(run["id"])
            publication = self.database.get_publication(run["id"])
            if (run["state"] != "VERIFIED" or draft is None or
                    draft["state"] != "CONFIRMED" or publication is None or
                    publication["state"] != "CONFIRMED"):
                raise ObservationConflict("exact verified draft publication is absent")
            self.database.pin_observation_policy(run["id"], self.observation_policy)
            head = self.database.seed_pr_head(run, draft, publication)
            self._exact_pull(run, draft, head, publication)
            checks = self.github.check_runs(head["head_commit_sha"])
            reviews = self.github.pull_reviews(draft["pr_number"])
            comments = self.github.pull_review_comments(draft["pr_number"])
            self._exact_pull(run, draft, head, publication)
            clean_checks, check_findings, check_states = self._checks(head["head_commit_sha"], checks)
            clean_reviews, review_findings, review_states = self._reviews(head["head_commit_sha"], reviews)
            clean_comments, comment_findings, comment_states = self._comments(head["head_commit_sha"], comments)
            if interrupt_after_read:
                os._exit(75)
            findings = check_findings + review_findings + comment_findings
            states = check_states + review_states + comment_states
            gate = next((status for status in ("NEEDS_HUMAN", "FAIL", "PENDING") if status in states), "PASS")
            payload = {"checks": clean_checks, "reviews": clean_reviews,
                       "comments": clean_comments,
                       "gate": gate, "findings": findings}
            return self.database.save_pr_observation(
                head, self.observation_policy, canonical_digest(payload),
                gate, findings, clean_checks, clean_reviews, clean_comments)
