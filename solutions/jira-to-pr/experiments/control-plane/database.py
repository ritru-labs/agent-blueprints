"""PostgreSQL is the operational source of truth for Phase 1C."""

import hashlib
import json
import pathlib
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

HERE = pathlib.Path(__file__).resolve().parent


class BusyRun(RuntimeError):
    pass


class Store:
    def __init__(self, dsn):
        if not dsn:
            raise ValueError("PHASE1C_DATABASE_URL is required")
        self.dsn = dsn

    def connect(self, *, autocommit=False):
        return psycopg.connect(self.dsn, autocommit=autocommit, row_factory=dict_row,
                               connect_timeout=5)

    def migrate(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.schema_migrations') AS name")
                if cur.fetchone()["name"] is None:
                    cur.execute((HERE / "schema.sql").read_text(), prepare=False)
                cur.execute("SELECT array_agg(version ORDER BY version) AS versions FROM schema_migrations")
                versions = cur.fetchone()["versions"]
                if versions == [1]:
                    cur.execute((HERE / "schema_v2.sql").read_text(), prepare=False)
                    versions = [1, 2]
                if versions == [1, 2]:
                    cur.execute((HERE / "schema_v3.sql").read_text(), prepare=False)
                    versions = [1, 2, 3]
                if versions == [1, 2, 3]:
                    cur.execute((HERE / "schema_v4.sql").read_text(), prepare=False)
                elif versions != [1, 2, 3, 4]:
                    raise RuntimeError("unsupported control-plane schema version")

    @contextmanager
    def worker_lock(self, run_id):
        key = int.from_bytes(hashlib.sha256(str(run_id).encode()).digest()[:8], "big", signed=True)
        with self.connect(autocommit=True) as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,)).fetchone()["locked"]
            if not acquired:
                raise BusyRun("another worker is recovering this run")
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def create_run(self, task_key, session_id, policy):
        doc = policy.document
        with self.connect() as conn:
            conn.execute("INSERT INTO policy_snapshots(policy_hash, document) VALUES (%s, %s) "
                         "ON CONFLICT (policy_hash) DO NOTHING", (policy.sha256, Jsonb(doc)))
            saved = conn.execute("SELECT document FROM policy_snapshots WHERE policy_hash = %s",
                                 (policy.sha256,)).fetchone()
            if saved["document"] != doc:
                raise ValueError("stored policy snapshot differs from local policy")
            run_id = uuid.uuid4()
            inserted = conn.execute(
                "INSERT INTO workflow_runs(id, task_key, repository, base_commit, session_id, "
                "current_session_id, policy_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (task_key) DO NOTHING RETURNING id",
                (run_id, task_key, doc["repository"], doc["base_commit"], session_id,
                 session_id, policy.sha256),
            ).fetchone()
            run = conn.execute("SELECT * FROM workflow_runs WHERE task_key = %s FOR UPDATE",
                               (task_key,)).fetchone()
            if (run["repository"] != doc["repository"] or run["base_commit"] != doc["base_commit"] or
                    run["session_id"] != session_id or run["policy_hash"] != policy.sha256):
                raise ValueError("existing task is bound to different session, base, or policy")
            if inserted:
                conn.execute("INSERT INTO workflow_sessions(session_id, run_id) VALUES (%s, %s)",
                             (session_id, run["id"]))
                self._event(conn, run["id"], None, "RECEIVED", "trusted synthetic task admitted")
            return run

    def get_run(self, task_key):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE task_key = %s", (task_key,)).fetchone()
            if run is None:
                raise KeyError(f"unknown task: {task_key}")
            return run

    def assert_policy(self, run, policy):
        if (run["policy_hash"] != policy.sha256 or
                run["repository"] != policy.document["repository"] or
                run["base_commit"] != policy.document["base_commit"]):
            raise ValueError("recovery policy differs from stored immutable policy")

    @staticmethod
    def _event(conn, run_id, before, after, reason):
        conn.execute("INSERT INTO workflow_events(run_id, from_state, to_state, reason) "
                     "VALUES (%s, %s, %s, %s)", (run_id, before, after, reason))

    def attach_candidate(self, run_id, session_id, turn_id, artifact_id,
                         archive_sha, tree_sha, base_commit, storage_name):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            candidate = self.get_candidate_for_connection(conn, run)
            if candidate and (candidate["source_session_id"], candidate["source_turn_id"],
                              candidate["source_artifact_id"], candidate["archive_sha256"],
                              candidate["tree_sha256"], candidate["base_commit"],
                              candidate["storage_path"]) == (session_id, turn_id, artifact_id,
                                                               archive_sha, tree_sha, base_commit,
                                                               storage_name):
                return candidate
            if run["state"] not in ("RECEIVED", "AWAITING_REPAIR_CANDIDATE"):
                raise ValueError("run cannot accept a candidate in its current state")
            if session_id != run["current_session_id"] or not turn_id:
                raise ValueError("candidate session or saved turn differs from current lineage")
            ordinal = 1 if candidate is None else candidate["ordinal"] + 1
            attempt = None
            if ordinal > 1:
                attempt = conn.execute(
                    "SELECT * FROM repair_attempts WHERE run_id = %s AND ordinal = %s",
                    (run_id, ordinal - 1),
                ).fetchone()
                if (attempt is None or attempt["status"] != "OBSERVED" or
                        attempt["result_turn_id"] != turn_id or attempt["session_id"] != session_id or
                        attempt["failed_verification_id"] != run["verification_id"]):
                    raise ValueError("candidate is not from the reconciled repair turn")
            candidate_id = uuid.uuid4()
            candidate = conn.execute(
                "INSERT INTO candidate_artifacts(id, run_id, source_artifact_id, archive_sha256, "
                "tree_sha256, base_commit, storage_path, ordinal, source_session_id, source_turn_id, "
                "repair_attempt_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (candidate_id, run_id, artifact_id, archive_sha, tree_sha, base_commit, storage_name,
                 ordinal, session_id, turn_id, attempt["id"] if attempt else None),
            ).fetchone()
            conn.execute("UPDATE workflow_runs SET state = 'CANDIDATE_READY', candidate_id = %s, "
                         "verification_id = NULL "
                         "WHERE id = %s", (candidate_id, run_id))
            self._event(conn, run_id, run["state"], "CANDIDATE_READY",
                        "content-addressed candidate committed")
            return candidate

    @staticmethod
    def get_candidate_for_connection(conn, run):
        if run["candidate_id"] is None:
            return None
        return conn.execute("SELECT * FROM candidate_artifacts WHERE id = %s AND run_id = %s",
                            (run["candidate_id"], run["id"])).fetchone()

    def get_candidate(self, run):
        with self.connect() as conn:
            return self.get_candidate_for_connection(conn, run)

    def get_verification(self, run):
        if run["verification_id"] is None:
            return None
        with self.connect() as conn:
            return conn.execute("SELECT * FROM verification_runs WHERE id = %s AND run_id = %s",
                                (run["verification_id"], run["id"])).fetchone()

    def begin_verification(self, run_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] == "CANDIDATE_READY":
                conn.execute("UPDATE workflow_runs SET state = 'VERIFYING' WHERE id = %s", (run_id,))
                self._event(conn, run_id, "CANDIDATE_READY", "VERIFYING",
                            "trusted verification started")
            elif run["state"] != "VERIFYING":
                raise ValueError(f"cannot verify from {run['state']}")

    def finish_verification(self, run_id, candidate, evidence):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] in ("VERIFIED", "NEEDS_HUMAN", "REPAIR_PENDING"):
                if run["candidate_id"] != candidate["id"]:
                    raise ValueError("completed verification belongs to another candidate")
                return run
            if run["state"] != "VERIFYING" or run["candidate_id"] != candidate["id"]:
                raise ValueError("verification does not target the current candidate")
            verification_id = uuid.uuid4()
            status = evidence["status"]
            conn.execute(
                "INSERT INTO verification_runs(id, run_id, candidate_id, archive_sha256, tree_sha256, "
                "base_commit, status, verifier_image_id, evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (verification_id, run_id, candidate["id"], evidence["artifact_sha256"],
                 evidence["candidate_tree_sha256"], evidence["base_commit"], status,
                 evidence["verifier_image_id"], Jsonb(evidence)),
            )
            if status == "PASS":
                next_state = "VERIFIED"
            else:
                policy = conn.execute("SELECT document FROM policy_snapshots WHERE policy_hash = %s",
                                      (run["policy_hash"],)).fetchone()["document"]
                used = conn.execute("SELECT count(*) AS n FROM repair_attempts WHERE run_id = %s",
                                    (run_id,)).fetchone()["n"]
                repairable = evidence.get("findings") == [{
                    "code": "ADD_ARITHMETIC",
                    "message": "add(a, b) must return the arithmetic sum for positive, zero, and negative integers.",
                }]
                next_state = ("REPAIR_PENDING" if used < policy["max_repair_attempts"] and
                              repairable else "NEEDS_HUMAN")
            run = conn.execute("UPDATE workflow_runs SET state = %s, verification_id = %s "
                               "WHERE id = %s RETURNING *", (next_state, verification_id, run_id)).fetchone()
            self._event(conn, run_id, "VERIFYING", next_state,
                        "trusted verification persisted for exact candidate")
            return run

    def replace_session(self, run_id, predecessor_id, replacement_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] != "REPAIR_PENDING" or run["current_session_id"] != predecessor_id:
                raise ValueError("replacement session requires current failed session")
            conn.execute("INSERT INTO workflow_sessions(session_id, run_id, predecessor_session_id) "
                         "VALUES (%s, %s, %s)", (replacement_id, run_id, predecessor_id))
            conn.execute("UPDATE workflow_runs SET current_session_id = %s WHERE id = %s",
                         (replacement_id, run_id))
            self._event(conn, run_id, "REPAIR_PENDING", "REPAIR_PENDING",
                        "replacement session linked to predecessor")

    def plan_repair(self, run_id, input_key, input_sha256):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] != "REPAIR_PENDING":
                raise ValueError("repair can only be planned for a failed verification")
            ordinal = conn.execute("SELECT count(*) + 1 AS n FROM repair_attempts WHERE run_id = %s",
                                   (run_id,)).fetchone()["n"]
            attempt = conn.execute(
                "INSERT INTO repair_attempts(id, run_id, ordinal, failed_candidate_id, "
                "failed_verification_id, session_id, input_key, input_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), run_id, ordinal, run["candidate_id"], run["verification_id"],
                 run["current_session_id"], input_key, input_sha256),
            ).fetchone()
            conn.execute("UPDATE workflow_runs SET state = 'REPAIR_INPUT_PLANNED' WHERE id = %s", (run_id,))
            self._event(conn, run_id, "REPAIR_PENDING", "REPAIR_INPUT_PLANNED",
                        "sanitized repair input planned")
            return attempt

    def current_repair(self, run_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM repair_attempts WHERE run_id = %s "
                                "ORDER BY ordinal DESC LIMIT 1", (run_id,)).fetchone()

    def mark_repair_uncertain(self, run_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            attempt = conn.execute("SELECT * FROM repair_attempts WHERE run_id = %s "
                                   "ORDER BY ordinal DESC LIMIT 1 FOR UPDATE", (run_id,)).fetchone()
            if attempt is None:
                raise ValueError("run has no repair input intent")
            if run["state"] == "REPAIR_INPUT_UNKNOWN" and attempt["status"] == "UNCERTAIN":
                return attempt
            if run["state"] != "REPAIR_INPUT_PLANNED" or attempt["status"] != "PLANNED":
                raise ValueError("repair input cannot be marked uncertain twice")
            attempt = conn.execute("UPDATE repair_attempts SET status = 'UNCERTAIN' "
                                   "WHERE id = %s RETURNING *", (attempt["id"],)).fetchone()
            conn.execute("UPDATE workflow_runs SET state = 'REPAIR_INPUT_UNKNOWN' WHERE id = %s", (run_id,))
            self._event(conn, run_id, "REPAIR_INPUT_PLANNED", "REPAIR_INPUT_UNKNOWN",
                        "repair input may have been submitted; reconcile before retry")
            return attempt

    def observe_repair(self, run_id, session_id, input_sha256, message_item_id, result_turn_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            attempt = conn.execute("SELECT * FROM repair_attempts WHERE run_id = %s "
                                   "ORDER BY ordinal DESC LIMIT 1 FOR UPDATE", (run_id,)).fetchone()
            if attempt is None:
                raise ValueError("run has no repair input intent")
            expected = (attempt["session_id"], attempt["input_sha256"])
            if expected != (session_id, input_sha256) or not message_item_id or not result_turn_id:
                raise ValueError("saved repair message does not match durable input intent")
            if run["state"] == "AWAITING_REPAIR_CANDIDATE" and attempt["status"] == "OBSERVED":
                if (attempt["message_item_id"], attempt["result_turn_id"]) != (message_item_id, result_turn_id):
                    raise ValueError("conflicting saved repair observation")
                return attempt
            if run["state"] != "REPAIR_INPUT_UNKNOWN" or attempt["status"] != "UNCERTAIN":
                raise ValueError("repair observation is not expected")
            attempt = conn.execute("UPDATE repair_attempts SET status = 'OBSERVED', "
                                   "message_item_id = %s, result_turn_id = %s "
                                   "WHERE id = %s RETURNING *",
                                   (message_item_id, result_turn_id, attempt["id"])).fetchone()
            conn.execute("UPDATE workflow_runs SET state = 'AWAITING_REPAIR_CANDIDATE' "
                         "WHERE id = %s", (run_id,))
            self._event(conn, run_id, "REPAIR_INPUT_UNKNOWN", "AWAITING_REPAIR_CANDIDATE",
                        "saved session message and repair turn reconciled")
            return attempt

    def needs_human(self, run_id, reason):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] == "NEEDS_HUMAN":
                return run
            before = run["state"]
            run = conn.execute("UPDATE workflow_runs SET state = 'NEEDS_HUMAN' "
                               "WHERE id = %s RETURNING *", (run_id,)).fetchone()
            self._event(conn, run_id, before, "NEEDS_HUMAN", reason[:200])
            return run

    def reserve_synthetic_operation(self, run_id, key, payload_sha256):
        with self.connect() as conn:
            run = conn.execute("SELECT state FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run is None or run["state"] != "VERIFIED":
                raise ValueError("synthetic operation requires a verified run")
            inserted = conn.execute(
                "INSERT INTO external_operations(id, run_id, operation_key, kind, payload_sha256) "
                "VALUES (%s, %s, %s, 'synthetic_notice', %s) "
                "ON CONFLICT (operation_key) DO NOTHING RETURNING *",
                (uuid.uuid4(), run_id, key, payload_sha256),
            ).fetchone()
            operation = inserted or conn.execute(
                "SELECT * FROM external_operations WHERE operation_key = %s FOR UPDATE", (key,),
            ).fetchone()
            if (operation["run_id"] != run_id or operation["kind"] != "synthetic_notice" or
                    operation["payload_sha256"] != payload_sha256):
                raise ValueError("idempotency key reused with different intent")
            return operation

    def mark_synthetic_operation_succeeded(self, operation_id):
        with self.connect() as conn:
            operation = conn.execute("SELECT * FROM external_operations WHERE id = %s FOR UPDATE",
                                     (operation_id,)).fetchone()
            if operation is None:
                raise KeyError("unknown operation")
            if operation["state"] == "PLANNED":
                return conn.execute("UPDATE external_operations SET state = 'SUCCEEDED' "
                                    "WHERE id = %s RETURNING *", (operation_id,)).fetchone()
            if operation["state"] == "SUCCEEDED":
                return operation
            raise ValueError("unknown outcome requires reconciliation; retry forbidden")

    def mark_synthetic_operation_unknown(self, operation_id):
        with self.connect() as conn:
            operation = conn.execute("SELECT * FROM external_operations WHERE id = %s FOR UPDATE",
                                     (operation_id,)).fetchone()
            if operation is None:
                raise KeyError("unknown operation")
            if operation["state"] == "PLANNED":
                return conn.execute("UPDATE external_operations SET state = 'OUTCOME_UNKNOWN' "
                                    "WHERE id = %s RETURNING *", (operation_id,)).fetchone()
            if operation["state"] == "OUTCOME_UNKNOWN":
                return operation
            raise ValueError("completed operation cannot become uncertain")

    def get_publication(self, run_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s",
                                (run_id,)).fetchone()

    def plan_publication(self, run_id, intent):
        fields = ("candidate_id", "verification_id", "archive_sha256",
                  "candidate_tree_sha256", "base_commit", "git_tree_sha",
                  "commit_sha", "branch_ref", "remote_id", "publisher_policy_hash",
                  "operation_key", "remote_kind")
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if run is None or run["state"] != "VERIFIED":
                raise ValueError("publication requires a verified run")
            inserted = conn.execute(
                "INSERT INTO publication_attempts(id, run_id, candidate_id, verification_id, "
                "archive_sha256, candidate_tree_sha256, base_commit, git_tree_sha, "
                "commit_sha, branch_ref, remote_id, publisher_policy_hash, operation_key, "
                "remote_kind) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO NOTHING RETURNING *",
                (uuid.uuid4(), run_id, *(intent[field] for field in fields)),
            ).fetchone()
            saved = inserted or conn.execute(
                "SELECT * FROM publication_attempts WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if any(str(saved[field]) != str(intent[field]) for field in fields):
                raise ValueError("stored publication intent differs from exact candidate or policy")
            return saved

    def mark_publication_unknown(self, run_id):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if row is None:
                raise ValueError("publication intent is missing")
            if row["state"] == "OUTCOME_UNKNOWN":
                return row
            if row["state"] != "PLANNED":
                raise ValueError("publication has already been confirmed")
            return conn.execute("UPDATE publication_attempts SET state = 'OUTCOME_UNKNOWN' "
                                "WHERE id = %s RETURNING *", (row["id"],)).fetchone()

    def confirm_publication(self, run_id):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if row is None:
                raise ValueError("publication intent is missing")
            if row["state"] == "CONFIRMED":
                return row
            if row["state"] != "OUTCOME_UNKNOWN":
                raise ValueError("publication must be uncertain before remote confirmation")
            return conn.execute("UPDATE publication_attempts SET state = 'CONFIRMED', "
                                "confirmed_at = clock_timestamp() WHERE id = %s RETURNING *",
                                (row["id"],)).fetchone()

    def get_draft_pr(self, run_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM draft_pr_attempts WHERE run_id = %s",
                                (run_id,)).fetchone()

    def plan_draft_pr(self, run_id, intent):
        fields = ("publication_id", "candidate_id", "verification_id", "repository",
                  "base_ref", "head_ref", "head_commit_sha", "actor_login",
                  "title_sha256", "body_sha256", "operation_key")
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if run is None or run["state"] != "VERIFIED":
                raise ValueError("draft PR requires a verified run")
            inserted = conn.execute(
                "INSERT INTO draft_pr_attempts(id, run_id, publication_id, candidate_id, "
                "verification_id, repository, base_ref, head_ref, head_commit_sha, "
                "actor_login, title_sha256, body_sha256, operation_key) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO NOTHING RETURNING *",
                (uuid.uuid4(), run_id, *(intent[field] for field in fields)),
            ).fetchone()
            saved = inserted or conn.execute(
                "SELECT * FROM draft_pr_attempts WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if any(str(saved[field]) != str(intent[field]) for field in fields):
                raise ValueError("stored draft PR intent differs from exact publication or policy")
            return saved

    def mark_draft_pr_unknown(self, run_id):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute("SELECT * FROM draft_pr_attempts WHERE run_id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if row is None:
                raise ValueError("draft PR intent is missing")
            if row["state"] == "OUTCOME_UNKNOWN":
                return row
            if row["state"] != "PLANNED":
                raise ValueError("draft PR has already been confirmed")
            return conn.execute("UPDATE draft_pr_attempts SET state = 'OUTCOME_UNKNOWN' "
                                "WHERE id = %s RETURNING *", (row["id"],)).fetchone()

    def confirm_draft_pr(self, run_id, pr_number, pr_url):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute("SELECT * FROM draft_pr_attempts WHERE run_id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            if row is None:
                raise ValueError("draft PR intent is missing")
            if row["state"] == "CONFIRMED":
                if (row["pr_number"], row["pr_url"]) != (pr_number, pr_url):
                    raise ValueError("confirmed draft PR identity changed")
                return row
            if row["state"] != "OUTCOME_UNKNOWN":
                raise ValueError("draft PR must be uncertain before remote confirmation")
            return conn.execute("UPDATE draft_pr_attempts SET state = 'CONFIRMED', "
                                "pr_number = %s, pr_url = %s, confirmed_at = clock_timestamp() "
                                "WHERE id = %s RETURNING *",
                                (pr_number, pr_url, row["id"])).fetchone()

    def summary(self, task_key):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE task_key = %s", (task_key,)).fetchone()
            if run is None:
                raise KeyError(f"unknown task: {task_key}")
            candidate = self.get_candidate_for_connection(conn, run)
            verification = (conn.execute("SELECT * FROM verification_runs WHERE id = %s",
                                         (run["verification_id"],)).fetchone()
                            if run["verification_id"] else None)
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM candidate_artifacts WHERE run_id = %s) AS candidates, "
                "(SELECT count(*) FROM verification_runs WHERE run_id = %s) AS verifications, "
                "(SELECT count(*) FROM repair_attempts WHERE run_id = %s) AS repairs, "
                "(SELECT count(*) FROM workflow_sessions WHERE run_id = %s) AS sessions, "
                "(SELECT count(*) FROM workflow_events WHERE run_id = %s) AS events",
                (run["id"], run["id"], run["id"], run["id"], run["id"]),
            ).fetchone()
            publication = conn.execute(
                "SELECT state, branch_ref, commit_sha, git_tree_sha FROM publication_attempts "
                "WHERE run_id = %s", (run["id"],),
            ).fetchone()
            draft = conn.execute(
                "SELECT state, pr_number, pr_url FROM draft_pr_attempts WHERE run_id = %s",
                (run["id"],),
            ).fetchone()
            return {
                "task_key": task_key, "run_id": str(run["id"]), "state": run["state"],
                "session_id": run["session_id"], "current_session_id": run["current_session_id"],
                "policy_sha256": run["policy_hash"],
                "base_commit": run["base_commit"], "version": run["version"],
                "candidate_id": str(candidate["id"]) if candidate else None,
                "artifact_sha256": candidate["archive_sha256"] if candidate else None,
                "candidate_tree_sha256": candidate["tree_sha256"] if candidate else None,
                "verification_id": str(verification["id"]) if verification else None,
                "verification_status": verification["status"] if verification else None,
                "candidate_count": counts["candidates"],
                "candidate_ordinal": candidate["ordinal"] if candidate else None,
                "verification_count": counts["verifications"],
                "repair_count": counts["repairs"], "session_count": counts["sessions"],
                "event_count": counts["events"],
                "publication_state": publication["state"] if publication else None,
                "published_ref": publication["branch_ref"] if publication else None,
                "published_commit_sha": publication["commit_sha"] if publication else None,
                "published_git_tree_sha": publication["git_tree_sha"] if publication else None,
                "draft_pr_state": draft["state"] if draft else None,
                "draft_pr_number": draft["pr_number"] if draft else None,
                "draft_pr_url": draft["pr_url"] if draft else None,
            }

    def history(self, task_key):
        run = self.get_run(task_key)
        with self.connect() as conn:
            candidates = conn.execute(
                "SELECT id, ordinal, source_session_id, source_turn_id, source_artifact_id, "
                "archive_sha256, tree_sha256, repair_attempt_id FROM candidate_artifacts "
                "WHERE run_id = %s ORDER BY ordinal", (run["id"],)).fetchall()
            verifications = conn.execute(
                "SELECT id, candidate_id, status, archive_sha256, tree_sha256, evidence "
                "FROM verification_runs WHERE run_id = %s ORDER BY created_at, id",
                (run["id"],)).fetchall()
            repairs = conn.execute(
                "SELECT id, ordinal, failed_candidate_id, failed_verification_id, session_id, "
                "input_key, input_sha256, status, message_item_id, result_turn_id "
                "FROM repair_attempts WHERE run_id = %s ORDER BY ordinal", (run["id"],)).fetchall()
            sessions = conn.execute(
                "SELECT session_id, predecessor_session_id FROM workflow_sessions "
                "WHERE run_id = %s ORDER BY created_at, session_id", (run["id"],)).fetchall()
        def clean(rows):
            return [{key: str(value) if isinstance(value, uuid.UUID) else value
                     for key, value in row.items()} for row in rows]
        return {"run": self.summary(task_key), "sessions": clean(sessions),
                "candidates": clean(candidates), "verifications": clean(verifications),
                "repairs": clean(repairs)}
