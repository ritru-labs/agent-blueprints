"""Deterministic workflow controller; model/session output cannot change state."""

import hashlib
import subprocess
import sys

from adapters import EXPERIMENTS, reconstruct_tree_hash

sys.path.insert(0, str(EXPERIMENTS / "agents-api-spike/repair-loop"))
from trusted_verify import sanitize as sanitize_findings  # noqa: E402


class Controller:
    def __init__(self, database, policy, artifact_store, verifier):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.verifier = verifier

    def admit_candidate(self, task_key, source):
        incoming = source.read()
        try:
            run = self.database.get_run(task_key)
        except KeyError:
            run = self.database.create_run(task_key, incoming.session_id, self.policy)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if incoming.session_id != run["current_session_id"]:
                raise ValueError("candidate session differs from stored current session")
            ci_repair = self.database.current_ci_repair(run["id"]) if run["state"] == "VERIFIED" else None
            ci_candidate = (ci_repair is not None and ci_repair["status"] == "OBSERVED" and
                            ci_repair["failed_candidate_id"] == run["candidate_id"] and
                            ci_repair["failed_verification_id"] == run["verification_id"] and
                            ci_repair["result_turn_id"] == incoming.turn_id)
            if run["state"] not in ("RECEIVED", "AWAITING_REPAIR_CANDIDATE") and not ci_candidate:
                candidate = self.database.get_candidate(run)
                if (candidate is None or candidate["source_turn_id"] != incoming.turn_id or
                        candidate["source_artifact_id"] != incoming.artifact_id or
                        candidate["archive_sha256"] != incoming.archive_sha256):
                    raise ValueError("existing run has a different candidate")
                return self.database.summary(task_key)
            storage_name = self.artifact_store.put(incoming.archive_bytes, incoming.archive_sha256)
            archive_path = self.artifact_store.checked_path(incoming.archive_sha256, storage_name)
            tree_hash = reconstruct_tree_hash(
                archive_path, incoming.archive_sha256, self.policy.document["base_commit"])
            self.database.attach_candidate(run["id"], incoming.session_id, incoming.turn_id,
                                           incoming.artifact_id,
                                           incoming.archive_sha256, tree_hash,
                                           self.policy.document["base_commit"], storage_name)
            return self.database.summary(task_key)

    @staticmethod
    def _ci_repair_message(run, observation):
        findings = observation["findings"]
        if observation["gate"] != "FAIL" or not findings:
            raise ValueError("CI/review observation has no approved repair finding")
        fragments = []
        for finding in findings:
            code = finding.get("code")
            if code == "REQUIRED_CHECK_FAILED" and finding.get("conclusion") == "failure":
                name = finding.get("check")
                if not isinstance(name, str) or len(name) > 120:
                    raise ValueError("required check identity is not bounded")
                fragments.append(f"required check {name} failed")
                repair_code = finding.get("repair_code")
                if repair_code == "REMOVE_PHASE1F_MARKER":
                    fragments.append("remove the PHASE1F_REPAIR_REQUIRED marker comment from sample/app.py")
                elif repair_code is not None:
                    raise ValueError("CI repair guidance code is not approved")
            elif code == "ADD_ARITHMETIC":
                fragments.append("add(a, b) must return the arithmetic sum")
            else:
                raise ValueError("CI/review finding is not approved for automatic repair")
        return (
            f"Trusted CI/review observation for exact draft PR head {observation['head_commit_sha']} "
            f"reported: {', '.join(fragments)}. Inspect the synthetic sample project and "
            "repair only sample/app.py and sample/tests/test_app.py. Run the candidate tests "
            "from /workspace, then create a fresh /workspace/outputs/sample-project.zip "
            "containing only sample/__init__.py, sample/app.py, and "
            "sample/tests/test_app.py. Read back the ZIP listing. This is a new candidate; "
            "do not claim trusted verification or CI passed."
        )

    def plan_ci_repair(self, task_key):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if run["state"] != "VERIFIED":
                raise ValueError("CI repair requires current VERIFIED candidate")
            prior = self.database.current_ci_repair(run["id"])
            if prior is not None and prior["failed_candidate_id"] != run["candidate_id"]:
                prior = None
            observation = (self.database.get_pr_observation(prior["observation_id"]) if prior
                           else self.database.latest_pr_observation(run["id"]))
            if observation is None or observation["candidate_id"] != run["candidate_id"] or \
                    observation["verification_id"] != run["verification_id"]:
                raise ValueError("current candidate has no exact-head CI finding")
            message = self._ci_repair_message(run, observation)
            digest = hashlib.sha256(message.encode()).hexdigest()
            key = hashlib.sha256(
                f"{run['id']}:{observation['id']}:{run['candidate_id']}:ci-repair".encode()
            ).hexdigest()
            if prior is None:
                used = self.database.repair_count(run["id"])
                if used >= self.policy.document["max_repair_attempts"]:
                    self.database.needs_human(run["id"], "CI repair budget exhausted")
                    raise ValueError("CI repair budget exhausted; human attention required")
            attempt = self.database.plan_ci_repair(run["id"], observation["id"], key, digest)
            return {"session_id": attempt["session_id"], "input_key": attempt["input_key"],
                    "input_sha256": attempt["input_sha256"], "status": attempt["status"],
                    "message": message}

    def mark_ci_repair_uncertain(self, task_key):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            self.database.assert_policy(self.database.get_run(task_key), self.policy)
            return self.database.mark_ci_repair_uncertain(run["id"])

    def observe_ci_repair(self, task_key, session_id, input_sha256, message_item_id, result_turn_id):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            self.database.assert_policy(self.database.get_run(task_key), self.policy)
            return self.database.observe_ci_repair(run["id"], session_id, input_sha256,
                                                   message_item_id, result_turn_id)

    def _sanitized_verification(self, record, run, candidate):
        required = {
            "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_commit": candidate["base_commit"],
            "verifier_image_id": self.policy.document["verifier_image_id"],
            "source_session_id": candidate["source_session_id"],
            "source_artifact_id": candidate["source_artifact_id"],
        }
        for key, expected in required.items():
            if record.get(key) != expected:
                raise ValueError(f"trusted verifier {key} does not match the stored candidate")
        if record.get("status") not in ("PASS", "FAIL"):
            raise ValueError("trusted verifier returned no final status")
        if record.get("isolation", {}).get("network") != "none":
            raise ValueError("trusted verifier did not report the pinned isolation")
        checks = record.get("checks", [])
        safe_checks = []
        for check in checks:
            if check.get("name") not in ("trusted_requirement", "candidate_tests"):
                raise ValueError("unknown trusted verifier check")
            safe_checks.append({
                "name": check["name"], "exit_code": check.get("exit_code"),
                "test_count": check.get("test_count"), "timed_out": check.get("timed_out"),
                "ok": check.get("ok"),
            })
        if record["status"] == "PASS":
            if (len(safe_checks) != 2 or
                    [x["name"] for x in safe_checks] != ["trusted_requirement", "candidate_tests"] or
                    safe_checks[0]["test_count"] != 5 or
                    not isinstance(safe_checks[1]["test_count"], int) or
                    safe_checks[1]["test_count"] < 1 or
                    any(x["exit_code"] != 0 or x["ok"] is not True or
                        x["timed_out"] is not False for x in safe_checks)):
                raise ValueError("verifier PASS lacks required test evidence")
        findings = sanitize_findings(record)["findings"]
        return {
            "schema_version": 1, "status": record["status"],
            "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_commit": candidate["base_commit"],
            "verifier_image_id": record["verifier_image_id"],
            "source_session_id": run["session_id"],
            "source_artifact_id": candidate["source_artifact_id"],
            "verified_at_utc": record.get("verified_at_utc"),
            "checks": safe_checks, "findings": findings,
        }

    @staticmethod
    def _repair_message(run, candidate, verification, ordinal):
        findings = verification["evidence"].get("findings", [])
        if (len(findings) != 1 or findings[0].get("code") != "ADD_ARITHMETIC" or
                verification["status"] != "FAIL"):
            raise ValueError("no approved sanitized repair finding")
        return (f"The independent trusted verifier rejected artifact SHA-256 "
                f"{candidate['archive_sha256']}. Finding ADD_ARITHMETIC: "
                "add(a, b) must return the arithmetic sum for positive, zero, and "
                "negative integers. Repair /workspace/sample/app.py, add regression "
                "tests in /workspace/sample/tests/test_app.py, run "
                "python3 -m unittest discover -s sample/tests -v from /workspace, "
                "then create a fresh /workspace/outputs/sample-project.zip ZIP with "
                "only sample/__init__.py, sample/app.py, and sample/tests/test_app.py. "
                "Read back the ZIP listing. This is a new candidate; do not claim "
                "trusted verification passed.")

    def plan_repair(self, task_key):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if run["state"] not in ("REPAIR_PENDING", "REPAIR_INPUT_PLANNED",
                                    "REPAIR_INPUT_UNKNOWN", "AWAITING_REPAIR_CANDIDATE"):
                raise ValueError("run has no failed candidate awaiting repair")
            candidate = self.database.get_candidate(run)
            verification = self.database.get_verification(run)
            prior = self.database.current_repair(run["id"])
            ordinal = prior["ordinal"] if run["state"] != "REPAIR_PENDING" else (
                1 if prior is None else prior["ordinal"] + 1)
            message = self._repair_message(run, candidate, verification, ordinal)
            digest = hashlib.sha256(message.encode()).hexdigest()
            key = hashlib.sha256(f"{run['id']}:{run['verification_id']}:{ordinal}:repair".encode()).hexdigest()
            if run["state"] == "REPAIR_PENDING":
                attempt = self.database.plan_repair(run["id"], key, digest)
            else:
                attempt = prior
                if (attempt["input_key"], attempt["input_sha256"]) != (key, digest):
                    raise ValueError("stored repair intent differs from deterministic input")
            return {"session_id": attempt["session_id"], "input_key": attempt["input_key"],
                    "input_sha256": attempt["input_sha256"], "ordinal": attempt["ordinal"],
                    "status": attempt["status"], "message": message}

    def mark_repair_uncertain(self, task_key):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            self.database.assert_policy(self.database.get_run(task_key), self.policy)
            return self.database.mark_repair_uncertain(run["id"])

    def observe_repair(self, task_key, session_id, input_sha256, message_item_id, result_turn_id):
        run = self.database.get_run(task_key)
        with self.database.worker_lock(run["id"]):
            self.database.assert_policy(self.database.get_run(task_key), self.policy)
            return self.database.observe_repair(run["id"], session_id, input_sha256,
                                                message_item_id, result_turn_id)

    def resume(self, task_key):
        run = self.database.get_run(task_key)
        self.database.assert_policy(run, self.policy)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if run["state"] == "NEEDS_HUMAN":
                return self.database.summary(task_key)
            if run["state"] == "RECEIVED":
                raise ValueError("run has no durable candidate; repeat admission with the source")
            candidate = self.database.get_candidate(run)
            if candidate is None:
                raise RuntimeError("candidate link is absent")
            try:
                archive_path = self.artifact_store.checked_path(
                    candidate["archive_sha256"], candidate["storage_path"])
                tree_hash = reconstruct_tree_hash(archive_path, candidate["archive_sha256"],
                                                  candidate["base_commit"])
                if tree_hash != candidate["tree_sha256"]:
                    raise ValueError("reconstructed candidate tree differs from stored hash")
            except (OSError, ValueError) as error:
                self.database.needs_human(run["id"], f"candidate integrity failure: {error}")
                return self.database.summary(task_key)
            if run["state"] == "VERIFIED" or run["state"].startswith("REPAIR_") or \
                    run["state"] == "AWAITING_REPAIR_CANDIDATE":
                return self.database.summary(task_key)
            self.database.begin_verification(run["id"])
            try:
                raw = self.verifier.verify(archive_path, candidate)
                evidence = self._sanitized_verification(raw, run, candidate)
                self.artifact_store.checked_path(candidate["archive_sha256"],
                                                 candidate["storage_path"])
                if reconstruct_tree_hash(archive_path, candidate["archive_sha256"],
                                         candidate["base_commit"]) != candidate["tree_sha256"]:
                    raise ValueError("candidate changed during trusted verification")
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                self.database.needs_human(run["id"], f"verifier could not establish result: {error}")
                return self.database.summary(task_key)
            self.database.finish_verification(run["id"], candidate, evidence)
            return self.database.summary(task_key)

    def reserve_synthetic_notice(self, task_key, payload):
        if "synthetic_notice" not in self.policy.document["allowed_operation_kinds"]:
            raise ValueError("synthetic operation not permitted")
        initial = self.database.get_run(task_key)
        if initial["state"] != "VERIFIED":
            raise ValueError("synthetic operation requires a verified run")
        current = self.resume(task_key)
        if current["state"] != "VERIFIED":
            raise ValueError("synthetic operation requires an intact verified candidate")
        run = self.database.get_run(task_key)
        self.database.assert_policy(run, self.policy)
        encoded = payload.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        key = f"{run['id']}:synthetic_notice:{run['candidate_id']}"
        return self.database.reserve_synthetic_operation(run["id"], key, digest)
