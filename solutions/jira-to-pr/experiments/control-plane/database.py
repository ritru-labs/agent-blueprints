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
                    versions = [1, 2, 3, 4]
                if versions == [1, 2, 3, 4]:
                    cur.execute((HERE / "schema_v5.sql").read_text(), prepare=False)
                    versions = [1, 2, 3, 4, 5]
                if versions == [1, 2, 3, 4, 5]:
                    cur.execute((HERE / "schema_v6.sql").read_text(), prepare=False)
                    versions = [1, 2, 3, 4, 5, 6]
                if versions == [1, 2, 3, 4, 5, 6]:
                    cur.execute((HERE / "schema_v7.sql").read_text(), prepare=False)
                elif versions != [1, 2, 3, 4, 5, 6, 7]:
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
            ci_repair = None
            if run["state"] == "VERIFIED":
                ci_repair = conn.execute(
                    "SELECT * FROM ci_repair_intents WHERE run_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)
                ).fetchone()
            if run["state"] not in ("RECEIVED", "AWAITING_REPAIR_CANDIDATE", "VERIFIED"):
                raise ValueError("run cannot accept a candidate in its current state")
            if run["state"] == "VERIFIED" and (
                    ci_repair is None or ci_repair["status"] != "OBSERVED" or
                    ci_repair["failed_candidate_id"] != run["candidate_id"] or
                    ci_repair["failed_verification_id"] != run["verification_id"] or
                    ci_repair["result_turn_id"] != turn_id):
                raise ValueError("new candidate lacks a reconciled CI repair turn")
            if session_id != run["current_session_id"] or not turn_id:
                raise ValueError("candidate session or saved turn differs from current lineage")
            ordinal = 1 if candidate is None else candidate["ordinal"] + 1
            attempt = None
            if ordinal > 1 and ci_repair is None:
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
                "repair_attempt_id, ci_repair_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (candidate_id, run_id, artifact_id, archive_sha, tree_sha, base_commit, storage_name,
                 ordinal, session_id, turn_id, attempt["id"] if attempt else None,
                 ci_repair["id"] if ci_repair else None),
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
                used = conn.execute(
                    "SELECT (SELECT count(*) FROM repair_attempts WHERE run_id = %s) + "
                    "(SELECT count(*) FROM ci_repair_intents WHERE run_id = %s) AS n",
                    (run_id, run_id)).fetchone()["n"]
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
            return conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s AND "
                                "candidate_id = (SELECT candidate_id FROM workflow_runs WHERE id = %s)",
                                (run_id, run_id)).fetchone()

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
                "ON CONFLICT (run_id, candidate_id) DO NOTHING RETURNING *",
                (uuid.uuid4(), run_id, *(intent[field] for field in fields)),
            ).fetchone()
            saved = inserted or conn.execute(
                "SELECT * FROM publication_attempts WHERE run_id = %s AND candidate_id = %s FOR UPDATE",
                (run_id, intent["candidate_id"]),
            ).fetchone()
            if any(str(saved[field]) != str(intent[field]) for field in fields):
                raise ValueError("stored publication intent differs from exact candidate or policy")
            return saved

    def mark_publication_unknown(self, run_id):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s AND "
                               "candidate_id = (SELECT candidate_id FROM workflow_runs WHERE id = %s) "
                               "FOR UPDATE", (run_id, run_id)).fetchone()
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
            row = conn.execute("SELECT * FROM publication_attempts WHERE run_id = %s AND "
                               "candidate_id = (SELECT candidate_id FROM workflow_runs WHERE id = %s) "
                               "FOR UPDATE", (run_id, run_id)).fetchone()
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
                "WHERE run_id = %s AND candidate_id = %s", (run["id"], run["candidate_id"]),
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

    def pin_observation_policy(self, run_id, policy):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            conn.execute("INSERT INTO pr_observation_policies(run_id, policy_sha256, document) "
                         "VALUES (%s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
                         (run_id, policy.sha256, Jsonb(policy.document)))
            saved = conn.execute("SELECT * FROM pr_observation_policies WHERE run_id = %s",
                                 (run_id,)).fetchone()
            if saved["policy_sha256"] != policy.sha256 or saved["document"] != policy.document:
                raise ValueError("Phase 1F observation policy drift")
            return saved

    def seed_pr_head(self, run, draft, publication):
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run["id"],))
            existing = conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s "
                                    "ORDER BY ordinal DESC LIMIT 1", (run["id"],)).fetchone()
            if existing:
                return existing
            if (draft["run_id"] != run["id"] or draft["publication_id"] != publication["id"] or
                    draft["head_commit_sha"] != publication["commit_sha"] or
                    publication["candidate_id"] != run["candidate_id"] or
                    publication["verification_id"] != run["verification_id"]):
                raise ValueError("initial PR head does not match current verified publication")
            return conn.execute(
                "INSERT INTO pr_head_links(id, run_id, draft_pr_id, publication_id, "
                "candidate_id, verification_id, head_commit_sha, ordinal) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1) RETURNING *",
                (uuid.uuid4(), run["id"], draft["id"], publication["id"],
                 run["candidate_id"], run["verification_id"], publication["commit_sha"]),
            ).fetchone()

    def save_pr_observation(self, head, policy, payload_sha, gate, findings, checks, reviews,
                            comments=()):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (head["run_id"],)).fetchone()
            latest = conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s "
                                  "ORDER BY ordinal DESC LIMIT 1", (head["run_id"],)).fetchone()
            if (run["state"] != "VERIFIED" or latest["id"] != head["id"] or
                    run["candidate_id"] != head["candidate_id"] or
                    run["verification_id"] != head["verification_id"]):
                raise ValueError("CI/review observation targets a stale candidate or PR head")
            pinned = conn.execute("SELECT * FROM pr_observation_policies WHERE run_id = %s",
                                  (run["id"],)).fetchone()
            if pinned["policy_sha256"] != policy.sha256:
                raise ValueError("CI/review observation policy drift")
            batch = conn.execute(
                "INSERT INTO pr_observation_batches(id, head_link_id, run_id, candidate_id, "
                "verification_id, publication_id, draft_pr_id, head_commit_sha, "
                "policy_sha256, payload_sha256, gate, findings) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (head_link_id, payload_sha256) DO NOTHING RETURNING *",
                (uuid.uuid4(), head["id"], head["run_id"], head["candidate_id"],
                 head["verification_id"], head["publication_id"], head["draft_pr_id"],
                 head["head_commit_sha"], policy.sha256, payload_sha, gate, Jsonb(findings)),
            ).fetchone()
            if batch is None:
                batch = conn.execute("SELECT * FROM pr_observation_batches WHERE "
                                     "head_link_id = %s AND payload_sha256 = %s",
                                     (head["id"], payload_sha)).fetchone()
                if batch["gate"] != gate or batch["findings"] != findings:
                    raise ValueError("observation digest collided with different findings")
                return batch
            for check in checks:
                conn.execute(
                    "INSERT INTO pr_check_observations(id, batch_id, check_name, app_slug, check_id, "
                    "run_attempt, check_suite_id, head_commit_sha, status, conclusion, "
                    "started_at, completed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), batch["id"], check["check_name"], check["app_slug"],
                     check["check_id"],
                     check["run_attempt"], check["check_suite_id"], check["head_commit_sha"],
                     check["status"], check["conclusion"], check["started_at"],
                     check["completed_at"]),
                )
            for review in reviews:
                conn.execute(
                    "INSERT INTO pr_review_observations(id, batch_id, review_id, reviewer, "
                    "review_head_sha, state, finding_code, body_sha256, submitted_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), batch["id"], review["review_id"], review["reviewer"],
                     review["review_head_sha"], review["state"], review["finding_code"],
                     review["body_sha256"], review["submitted_at"]),
                )
            for comment in comments:
                conn.execute(
                    "INSERT INTO pr_review_comment_observations(id, batch_id, comment_id, reviewer, "
                    "review_head_sha, finding_code, body_sha256, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), batch["id"], comment["comment_id"], comment["reviewer"],
                     comment["review_head_sha"], comment["finding_code"],
                     comment["body_sha256"], comment["created_at"]),
                )
            return batch

    def latest_pr_observation(self, run_id):
        with self.connect() as conn:
            return conn.execute(
                "SELECT b.* FROM pr_observation_batches b JOIN pr_head_links h "
                "ON h.id = b.head_link_id WHERE h.run_id = %s AND h.ordinal = "
                "(SELECT max(ordinal) FROM pr_head_links WHERE run_id = %s) "
                "ORDER BY b.observed_at DESC, b.id DESC LIMIT 1", (run_id, run_id),
            ).fetchone()

    def get_pr_observation(self, observation_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM pr_observation_batches WHERE id = %s",
                                (observation_id,)).fetchone()

    def latest_pr_head(self, run_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s "
                                "ORDER BY ordinal DESC LIMIT 1", (run_id,)).fetchone()

    def previous_pr_head(self, run_id, ordinal):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s AND ordinal = %s",
                                (run_id, ordinal - 1)).fetchone()

    def link_republication(self, run_id, publication_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            publication = conn.execute("SELECT * FROM publication_attempts WHERE id = %s",
                                       (publication_id,)).fetchone()
            draft = conn.execute("SELECT * FROM draft_pr_attempts WHERE run_id = %s",
                                 (run_id,)).fetchone()
            prior = conn.execute("SELECT * FROM pr_head_links WHERE run_id = %s "
                                 "ORDER BY ordinal DESC LIMIT 1", (run_id,)).fetchone()
            candidate = self.get_candidate_for_connection(conn, run)
            if (run["state"] != "VERIFIED" or publication is None or draft is None or prior is None or
                    publication["state"] != "CONFIRMED" or draft["state"] != "CONFIRMED" or
                    publication["candidate_id"] != run["candidate_id"] or
                    publication["verification_id"] != run["verification_id"] or
                    publication["branch_ref"] != draft["head_ref"]):
                raise ValueError("republication lacks the current exact verified draft identity")
            if prior["publication_id"] == publication_id:
                return prior
            ci = conn.execute("SELECT * FROM ci_repair_intents WHERE id = %s",
                              (candidate["ci_repair_id"],)).fetchone()
            if (ci is None or ci["status"] != "OBSERVED" or
                    ci["head_link_id"] != prior["id"] or
                    ci["failed_candidate_id"] != prior["candidate_id"] or
                    ci["failed_verification_id"] != prior["verification_id"]):
                raise ValueError("republication lacks the observed CI repair lineage")
            return conn.execute(
                "INSERT INTO pr_head_links(id, run_id, draft_pr_id, publication_id, "
                "candidate_id, verification_id, head_commit_sha, ordinal) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), run_id, draft["id"], publication_id, run["candidate_id"],
                 run["verification_id"], publication["commit_sha"], prior["ordinal"] + 1),
            ).fetchone()

    def get_pr_body_update(self, head_link_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM pr_body_update_attempts WHERE head_link_id = %s",
                                (head_link_id,)).fetchone()

    def prior_pr_body_sha(self, run_id, head):
        with self.connect() as conn:
            if head["ordinal"] == 2:
                row = conn.execute("SELECT body_sha256 FROM draft_pr_attempts WHERE run_id = %s",
                                   (run_id,)).fetchone()
                return row["body_sha256"] if row else None
            row = conn.execute(
                "SELECT u.body_sha256 FROM pr_body_update_attempts u JOIN pr_head_links h "
                "ON h.id = u.head_link_id WHERE h.run_id = %s AND h.ordinal = %s AND "
                "u.state = 'CONFIRMED'", (run_id, head["ordinal"] - 1),
            ).fetchone()
            return row["body_sha256"] if row else None

    def plan_pr_body_update(self, run_id, intent):
        fields = ("draft_pr_id", "head_link_id", "candidate_id", "verification_id",
                  "publication_id", "head_commit_sha", "previous_body_sha256",
                  "body_sha256", "operation_key")
        with self.connect() as conn:
            conn.execute("SELECT id FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,))
            inserted = conn.execute(
                "INSERT INTO pr_body_update_attempts(id, run_id, draft_pr_id, head_link_id, "
                "candidate_id, verification_id, publication_id, head_commit_sha, "
                "previous_body_sha256, body_sha256, operation_key) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (head_link_id) DO NOTHING RETURNING *",
                (uuid.uuid4(), run_id, *(intent[field] for field in fields)),
            ).fetchone()
            row = inserted or conn.execute("SELECT * FROM pr_body_update_attempts "
                                           "WHERE head_link_id = %s FOR UPDATE",
                                           (intent["head_link_id"],)).fetchone()
            if any(str(row[field]) != str(intent[field]) for field in fields):
                raise ValueError("stored PR body update differs from exact head intent")
            return row

    def mark_pr_body_update_unknown(self, head_link_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pr_body_update_attempts WHERE head_link_id = %s "
                               "FOR UPDATE", (head_link_id,)).fetchone()
            if row is None:
                raise ValueError("PR body update intent is absent")
            if row["state"] == "OUTCOME_UNKNOWN":
                return row
            if row["state"] != "PLANNED":
                raise ValueError("PR body update has already been confirmed")
            return conn.execute("UPDATE pr_body_update_attempts SET state = 'OUTCOME_UNKNOWN' "
                                "WHERE id = %s RETURNING *", (row["id"],)).fetchone()

    def confirm_pr_body_update(self, head_link_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pr_body_update_attempts WHERE head_link_id = %s "
                               "FOR UPDATE", (head_link_id,)).fetchone()
            if row is None:
                raise ValueError("PR body update intent is absent")
            if row["state"] == "CONFIRMED":
                return row
            if row["state"] != "OUTCOME_UNKNOWN":
                raise ValueError("PR body update requires uncertain remote outcome")
            return conn.execute("UPDATE pr_body_update_attempts SET state = 'CONFIRMED', "
                                "confirmed_at = clock_timestamp() WHERE id = %s RETURNING *",
                                (row["id"],)).fetchone()

    def repair_count(self, run_id):
        with self.connect() as conn:
            return conn.execute(
                "SELECT (SELECT count(*) FROM repair_attempts WHERE run_id = %s) + "
                "(SELECT count(*) FROM ci_repair_intents WHERE run_id = %s) AS n",
                (run_id, run_id)).fetchone()["n"]

    def current_ci_repair(self, run_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ci_repair_intents WHERE run_id = %s "
                                "ORDER BY created_at DESC, id DESC LIMIT 1", (run_id,)).fetchone()

    def plan_ci_repair(self, run_id, observation_id, input_key, input_sha256):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            observation = conn.execute("SELECT * FROM pr_observation_batches WHERE id = %s",
                                       (observation_id,)).fetchone()
            if run is None or observation is None or observation["run_id"] != run_id:
                raise ValueError("CI repair observation is absent")
            existing = conn.execute("SELECT * FROM ci_repair_intents WHERE "
                                    "head_link_id = %s FOR UPDATE",
                                    (observation["head_link_id"],)).fetchone()
            if existing:
                if (existing["observation_id"], existing["input_key"], existing["input_sha256"]) != (
                        observation_id, input_key, input_sha256):
                    raise ValueError("stored CI repair intent differs from deterministic input")
                return existing
            return conn.execute(
                "INSERT INTO ci_repair_intents(id, run_id, head_link_id, observation_id, "
                "failed_candidate_id, failed_verification_id, session_id, input_key, input_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), run_id, observation["head_link_id"], observation_id,
                 run["candidate_id"], run["verification_id"], run["current_session_id"],
                 input_key, input_sha256),
            ).fetchone()

    def mark_ci_repair_uncertain(self, run_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            row = conn.execute("SELECT * FROM ci_repair_intents WHERE run_id = %s "
                               "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
                               (run_id,)).fetchone()
            if (row is None or run["candidate_id"] != row["failed_candidate_id"] or
                    run["verification_id"] != row["failed_verification_id"] or
                    run["state"] != "VERIFIED"):
                raise ValueError("CI repair input is not current")
            if row["status"] == "UNCERTAIN":
                return row
            if row["status"] != "PLANNED":
                raise ValueError("CI repair input cannot be submitted twice")
            return conn.execute("UPDATE ci_repair_intents SET status = 'UNCERTAIN' "
                                "WHERE id = %s RETURNING *", (row["id"],)).fetchone()

    def observe_ci_repair(self, run_id, session_id, input_sha256, message_item_id, result_turn_id):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE",
                               (run_id,)).fetchone()
            row = conn.execute("SELECT * FROM ci_repair_intents WHERE run_id = %s "
                               "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
                               (run_id,)).fetchone()
            if (row is None or run["candidate_id"] != row["failed_candidate_id"] or
                    run["verification_id"] != row["failed_verification_id"] or
                    row["session_id"] != session_id or row["input_sha256"] != input_sha256 or
                    not message_item_id or not result_turn_id):
                raise ValueError("saved CI repair message differs from durable intent")
            if row["status"] == "OBSERVED":
                if (row["message_item_id"], row["result_turn_id"]) != (message_item_id, result_turn_id):
                    raise ValueError("conflicting saved CI repair turn")
                return row
            if row["status"] != "UNCERTAIN":
                raise ValueError("CI repair input must be uncertain before reconciliation")
            return conn.execute("UPDATE ci_repair_intents SET status = 'OBSERVED', "
                                "message_item_id = %s, result_turn_id = %s "
                                "WHERE id = %s RETURNING *",
                                (message_item_id, result_turn_id, row["id"])).fetchone()
