"""Deterministic workflow controller; model/session output cannot change state."""

import hashlib
import subprocess

from adapters import reconstruct_tree_hash


class Controller:
    def __init__(self, database, policy, artifact_store, verifier):
        self.database = database
        self.policy = policy
        self.artifact_store = artifact_store
        self.verifier = verifier

    def admit_candidate(self, task_key, source):
        incoming = source.read()
        run = self.database.create_run(task_key, incoming.session_id, self.policy)
        with self.database.worker_lock(run["id"]):
            run = self.database.get_run(task_key)
            self.database.assert_policy(run, self.policy)
            if run["state"] != "RECEIVED":
                candidate = self.database.get_candidate(run)
                if (candidate is None or candidate["source_artifact_id"] != incoming.artifact_id or
                        candidate["archive_sha256"] != incoming.archive_sha256):
                    raise ValueError("existing run has a different candidate")
                return self.database.summary(task_key)
            storage_name = self.artifact_store.put(incoming.archive_bytes, incoming.archive_sha256)
            archive_path = self.artifact_store.checked_path(incoming.archive_sha256, storage_name)
            tree_hash = reconstruct_tree_hash(
                archive_path, incoming.archive_sha256, self.policy.document["base_commit"])
            self.database.attach_candidate(run["id"], incoming.artifact_id,
                                           incoming.archive_sha256, tree_hash,
                                           self.policy.document["base_commit"], storage_name)
            return self.database.summary(task_key)

    def _sanitized_verification(self, record, run, candidate):
        required = {
            "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_commit": candidate["base_commit"],
            "verifier_image_id": self.policy.document["verifier_image_id"],
            "source_session_id": run["session_id"],
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
                "ok": check.get("ok"), "summary": str(check.get("summary", ""))[:100],
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
        return {
            "schema_version": 1, "status": record["status"],
            "artifact_sha256": candidate["archive_sha256"],
            "candidate_tree_sha256": candidate["tree_sha256"],
            "base_commit": candidate["base_commit"],
            "verifier_image_id": record["verifier_image_id"],
            "source_session_id": run["session_id"],
            "source_artifact_id": candidate["source_artifact_id"],
            "verified_at_utc": record.get("verified_at_utc"),
            "checks": safe_checks,
        }

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
            if run["state"] == "VERIFIED":
                return self.database.summary(task_key)
            self.database.begin_verification(run["id"])
            try:
                raw = self.verifier.verify(archive_path)
                evidence = self._sanitized_verification(raw, run, candidate)
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
