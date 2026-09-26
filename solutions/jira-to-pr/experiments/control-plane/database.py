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
                else:
                    cur.execute("SELECT array_agg(version ORDER BY version) AS versions FROM schema_migrations")
                    if cur.fetchone()["versions"] != [1]:
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
                "INSERT INTO workflow_runs(id, task_key, repository, base_commit, session_id, policy_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (task_key) DO NOTHING RETURNING id",
                (run_id, task_key, doc["repository"], doc["base_commit"], session_id, policy.sha256),
            ).fetchone()
            run = conn.execute("SELECT * FROM workflow_runs WHERE task_key = %s FOR UPDATE",
                               (task_key,)).fetchone()
            if (run["repository"] != doc["repository"] or run["base_commit"] != doc["base_commit"] or
                    run["session_id"] != session_id or run["policy_hash"] != policy.sha256):
                raise ValueError("existing task is bound to different session, base, or policy")
            if inserted:
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

    def attach_candidate(self, run_id, artifact_id, archive_sha, tree_sha, base_commit, storage_name):
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM workflow_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
            if run["state"] != "RECEIVED":
                candidate = self.get_candidate_for_connection(conn, run)
                if candidate and (candidate["source_artifact_id"], candidate["archive_sha256"],
                                  candidate["tree_sha256"], candidate["base_commit"],
                                  candidate["storage_path"]) == (artifact_id, archive_sha, tree_sha,
                                                                   base_commit, storage_name):
                    return candidate
                raise ValueError("run already has a different candidate")
            candidate_id = uuid.uuid4()
            candidate = conn.execute(
                "INSERT INTO candidate_artifacts(id, run_id, source_artifact_id, archive_sha256, "
                "tree_sha256, base_commit, storage_path) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (candidate_id, run_id, artifact_id, archive_sha, tree_sha, base_commit, storage_name),
            ).fetchone()
            conn.execute("UPDATE workflow_runs SET state = 'CANDIDATE_READY', candidate_id = %s "
                         "WHERE id = %s", (candidate_id, run_id))
            self._event(conn, run_id, "RECEIVED", "CANDIDATE_READY",
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
            if run["state"] in ("VERIFIED", "NEEDS_HUMAN"):
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
            next_state = "VERIFIED" if status == "PASS" else "NEEDS_HUMAN"
            run = conn.execute("UPDATE workflow_runs SET state = %s, verification_id = %s "
                               "WHERE id = %s RETURNING *", (next_state, verification_id, run_id)).fetchone()
            self._event(conn, run_id, "VERIFYING", next_state,
                        "trusted verification persisted for exact candidate")
            return run

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
                "(SELECT count(*) FROM workflow_events WHERE run_id = %s) AS events",
                (run["id"], run["id"], run["id"]),
            ).fetchone()
            return {
                "task_key": task_key, "run_id": str(run["id"]), "state": run["state"],
                "session_id": run["session_id"], "policy_sha256": run["policy_hash"],
                "base_commit": run["base_commit"], "version": run["version"],
                "candidate_id": str(candidate["id"]) if candidate else None,
                "artifact_sha256": candidate["archive_sha256"] if candidate else None,
                "candidate_tree_sha256": candidate["tree_sha256"] if candidate else None,
                "verification_id": str(verification["id"]) if verification else None,
                "verification_status": verification["status"] if verification else None,
                "candidate_count": counts["candidates"],
                "verification_count": counts["verifications"], "event_count": counts["events"],
            }
