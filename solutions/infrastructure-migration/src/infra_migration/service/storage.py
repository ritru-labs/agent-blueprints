"""Tenant-role PostgreSQL job queue and transactional execution ledger."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from ..ledger import ExecutionBinding, LocalLedger
from ..models import Principal, digest
from ..persistence import tenant_connection
from ..tools import AccessDenied

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_members (
 actor TEXT PRIMARY KEY, roles JSONB NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS service_jobs (
 id UUID PRIMARY KEY, requester TEXT NOT NULL, request_digest TEXT NOT NULL,
 idempotency_key UUID NOT NULL, payload JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED', result JSONB, review JSONB,
 lease UUID, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(requester,idempotency_key));
CREATE INDEX IF NOT EXISTS service_jobs_created_idx ON service_jobs(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS service_approvals (
 id TEXT PRIMARY KEY, binding_digest TEXT NOT NULL, approver TEXT NOT NULL,
 expires BIGINT NOT NULL, consumed BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS service_operations (
 id TEXT PRIMARY KEY, binding TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT, approval_id TEXT NOT NULL REFERENCES service_approvals(id));
CREATE TABLE IF NOT EXISTS service_locks (
 resource TEXT PRIMARY KEY, operation TEXT NOT NULL REFERENCES service_operations(id));
CREATE TABLE IF NOT EXISTS service_budgets (id TEXT PRIMARY KEY, used INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS service_rates (
 actor TEXT PRIMARY KEY, minute BIGINT NOT NULL, used INTEGER NOT NULL);
"""


class TenantStore:
    def __init__(self, dsn: str, tenant_id: UUID):
        self.dsn, self.tenant_id = dsn, tenant_id

    @contextmanager
    def connection(self):
        with tenant_connection(self.dsn, self.tenant_id) as db:
            yield db

    @contextmanager
    def transaction(self):
        with self.connection() as db, db.transaction():
            yield db

    def setup(self):
        # Explicit deployment action; request handling never creates roles or schemas.
        with self.transaction() as db:
            db.execute(SCHEMA, prepare=False)

    def ready(self):
        # A SELECT-only probe verifies restricted tenant role/schema and queue tables.
        with self.connection() as db:
            for table in ("service_members", "service_jobs", "service_operations"):
                db.execute("SELECT 1 FROM " + table + " LIMIT 0")
        return True

    def provision_member(self, actor: str, roles: tuple[str, ...], *, active=True):
        Principal(tenant_id=self.tenant_id, subject=actor, roles=roles)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO service_members VALUES(%s,%s,%s) "
                "ON CONFLICT(actor) DO UPDATE SET roles=EXCLUDED.roles, active=EXCLUDED.active",
                (actor, Jsonb(list(roles)), active),
            )

    def principal(self, actor: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT roles FROM service_members WHERE actor=%s AND active", (actor,)
            ).fetchone()
        if not row:
            raise AccessDenied("Organization membership required")
        return Principal(tenant_id=self.tenant_id, subject=actor, roles=tuple(row["roles"]))

    def _authorized(self, principal, role=None):
        if principal.tenant_id != self.tenant_id:
            raise AccessDenied("Tenant scope mismatch")
        current = self.principal(principal.subject)
        if not current.roles or (role and role not in current.roles):
            raise AccessDenied("Organization role required")
        return current

    def enqueue(self, principal, payload: dict, key: UUID):
        self._authorized(principal, "assessor")
        expected, job = digest(payload), uuid4()
        with self.transaction() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (lock_number(str(self.tenant_id)),))
            # Serialize retries for one actor/key, including transactions with no existing row.
            db.execute(
                "SELECT pg_advisory_xact_lock(%s)", (lock_number(principal.subject + str(key)),)
            )
            row = db.execute(
                "SELECT * FROM service_jobs WHERE requester=%s AND idempotency_key=%s",
                (principal.subject, key),
            ).fetchone()
            if row:
                if row["request_digest"] != expected:
                    raise AccessDenied("Idempotency key already binds a different request")
                return row
            active = db.execute(
                "SELECT count(*) AS n FROM service_jobs "
                "WHERE status IN ('QUEUED','RUNNING','REVIEW_QUEUED','REVIEW_RUNNING')"
            ).fetchone()["n"]
            if active >= 100:
                raise AccessDenied("Organization queue budget exhausted")
            return db.execute(
                "INSERT INTO service_jobs(id,requester,request_digest,idempotency_key,payload) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (job, principal.subject, expected, key, Jsonb(payload)),
            ).fetchone()

    def get(self, principal, job: UUID):
        self._authorized(principal)
        with self.connection() as db:
            row = db.execute("SELECT * FROM service_jobs WHERE id=%s", (job,)).fetchone()
        if not row:
            raise AccessDenied("Run unavailable")
        return row

    def list_runs(self, principal, *, before=None, limit=25):
        self._authorized(principal)
        if not 1 <= limit <= 50:
            raise ValueError("Run page size outside budget")
        with self.connection() as db:
            params = []
            condition = ""
            if before is not None:
                cursor = db.execute(
                    "SELECT created_at,id FROM service_jobs WHERE id=%s", (before,)
                ).fetchone()
                if not cursor:
                    raise AccessDenied("Run cursor unavailable")
                condition = "WHERE (created_at,id) < (%s,%s) "
                params.extend([cursor["created_at"], cursor["id"]])
            params.append(limit + 1)
            rows = db.execute(
                "SELECT id,requester,status,result,created_at FROM service_jobs "
                + condition
                + "ORDER BY created_at DESC,id DESC LIMIT %s",
                params,
            ).fetchall()
        return rows[:limit], rows[limit - 1]["id"] if len(rows) > limit else None

    def review(self, principal, job, decision):
        self._authorized(principal, "reviewer")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM service_jobs WHERE id=%s FOR UPDATE", (job,)).fetchone()
            if (
                not row
                or row["status"] != "AWAITING_REVIEW"
                or row["requester"] == principal.subject
                or row["result"]["review_digest"] != decision.plan_digest
            ):
                raise AccessDenied("Review unavailable, self-approved, or stale")
            review = {"actor": principal.subject, "decision": decision.model_dump(mode="json")}
            db.execute(
                "UPDATE service_jobs SET status='REVIEW_QUEUED', review=%s, attempts=0 WHERE id=%s",
                (Jsonb(review), job),
            )

    def cancel(self, principal, job):
        self._authorized(principal, "assessor")
        with self.transaction() as db:
            row = db.execute(
                "UPDATE service_jobs SET status='CANCELLED', lease=NULL, lease_until=NULL "
                "WHERE id=%s AND requester=%s AND status IN ('QUEUED','AWAITING_REVIEW') "
                "RETURNING id",
                (job, principal.subject),
            ).fetchone()
            if not row:
                raise AccessDenied("Run cannot be cancelled at this stage")

    def rate_limit(self, principal):
        self._authorized(principal)
        with self.transaction() as db:
            row = db.execute(
                "INSERT INTO service_rates VALUES(%s,"
                "floor(extract(epoch FROM clock_timestamp())/60),1) "
                "ON CONFLICT(actor) DO UPDATE SET minute=EXCLUDED.minute, "
                "used=CASE WHEN service_rates.minute=EXCLUDED.minute "
                "THEN service_rates.used+1 ELSE 1 END RETURNING used",
                (principal.subject,),
            ).fetchone()
        if row["used"] > 120:
            raise AccessDenied("Request budget exhausted")

    def claim(self, *, lease_seconds=120):
        if not 10 <= lease_seconds <= 600:
            raise ValueError("Invalid worker lease")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM service_jobs WHERE status IN ('QUEUED','REVIEW_QUEUED') "
                "OR (status IN ('RUNNING','REVIEW_RUNNING') AND lease_until < clock_timestamp()) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not row:
                return None
            if row["attempts"] >= 3:
                db.execute(
                    "UPDATE service_jobs SET status='FAILED',result=%s,lease=NULL WHERE id=%s",
                    (Jsonb({"error": "PREPARATION_RETRY_BUDGET_EXHAUSTED"}), row["id"]),
                )
                return None
            lease = uuid4()
            status = "REVIEW_RUNNING" if row["review"] else "RUNNING"
            return db.execute(
                "UPDATE service_jobs SET lease=%s, lease_until=clock_timestamp()+"
                "make_interval(secs=>%s), status=%s,attempts=attempts+1 WHERE id=%s RETURNING *",
                (lease, lease_seconds, status, row["id"]),
            ).fetchone()

    def finish(self, job, lease, status, result):
        if status not in {"AWAITING_REVIEW", "REVIEWED_EXECUTION_BLOCKED", "REJECTED", "FAILED"}:
            raise ValueError("Invalid terminal preparation status")
        with self.transaction() as db:
            row = db.execute(
                "UPDATE service_jobs SET status=%s,result=%s,lease=NULL,lease_until=NULL "
                "WHERE id=%s AND lease=%s AND lease_until>clock_timestamp() "
                "AND status IN ('RUNNING','REVIEW_RUNNING') RETURNING id",
                (status, Jsonb(result), job, lease),
            ).fetchone()
            if not row:
                raise AccessDenied("Worker lease expired or was superseded")

    @contextmanager
    def run_lock(self, job):
        with self.connection() as db:
            key = lock_number(str(job))
            if not db.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,)).fetchone()[
                "locked"
            ]:
                raise AccessDenied("Another worker still holds this preparation run")
            try:
                yield
            finally:
                db.execute("SELECT pg_advisory_unlock(%s)", (key,))


def lock_number(text):
    return int(digest({"lock": text})[:16], 16) - (1 << 63)


class PostgresLedger:
    """Execution-compatible ledger. It never grants adapter qualification or cloud authority."""

    authorize = staticmethod(LocalLedger.authorize)

    def __init__(self, store: TenantStore):
        self.store = store

    def _authorize(self, principal, binding, role):
        self.authorize(principal, binding, role)
        self.store._authorized(principal, role)

    def approve(self, principal, binding, *, ttl=300, now=None):
        self._authorize(principal, binding, "reviewer")
        if principal.subject == binding.requester or not 1 <= ttl <= 3600:
            raise AccessDenied("Invalid approval or self-approval")
        now = int(datetime.now(UTC).timestamp()) if now is None else now
        approval = str(uuid4())
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO service_approvals(id,binding_digest,approver,expires) "
                "VALUES(%s,%s,%s,%s)",
                (approval, digest(binding), principal.subject, now + ttl),
            )
        return approval

    def begin(self, principal, binding, approval, *, now=None):
        self._authorize(principal, binding, "executor")
        now = int(datetime.now(UTC).timestamp()) if now is None else now
        operation = str(uuid4())
        try:
            with self.store.transaction() as db:
                record = db.execute(
                    "SELECT * FROM service_approvals WHERE id=%s FOR UPDATE", (approval,)
                ).fetchone()
                if (
                    not record
                    or record["binding_digest"] != digest(binding)
                    or record["expires"] <= now
                    or record["consumed"]
                    or record["approver"] == principal.subject
                ):
                    raise AccessDenied("Approval expired, consumed, substituted or unauthorized")
                db.execute(
                    "INSERT INTO service_operations(id,binding,status,approval_id) "
                    "VALUES(%s,%s,'INTENT',%s)",
                    (operation, binding.model_dump_json(), approval),
                )
                for resource in sorted(set(binding.resources)):
                    key = digest(
                        {
                            "cloud": binding.scope.cloud,
                            "account": binding.scope.account_id,
                            "regions": sorted(binding.scope.regions),
                            "resource": resource,
                        }
                    )
                    db.execute("INSERT INTO service_locks VALUES(%s,%s)", (key, operation))
                db.execute("UPDATE service_approvals SET consumed=TRUE WHERE id=%s", (approval,))
        except psycopg.errors.UniqueViolation:
            raise AccessDenied("Resource locked by an unresolved operation") from None
        return operation

    def read(self, principal, operation):
        current = self.store._authorized(principal)
        if not set(current.roles) & {"reviewer", "executor"}:
            raise AccessDenied("Operation read role required")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM service_operations WHERE id=%s", (operation,)
            ).fetchone()
        if not row:
            raise AccessDenied("Operation unavailable")
        binding = ExecutionBinding.model_validate_json(row["binding"])
        if binding.scope.tenant_id != principal.tenant_id:
            raise AccessDenied("Operation tenant mismatch")
        return row

    def transition(self, principal, operation, expected, status, receipt=None):
        allowed = {
            "INTENT": {"SUBMITTED", "OUTCOME_UNKNOWN", "NO_EFFECT"},
            "SUBMITTED": {"OUTCOME_UNKNOWN", "SUCCEEDED"},
            "OUTCOME_UNKNOWN": {"SUCCEEDED", "NO_EFFECT"},
        }
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM service_operations WHERE id=%s FOR UPDATE", (operation,)
            ).fetchone()
            if not row:
                raise AccessDenied("Operation unavailable")
            binding = ExecutionBinding.model_validate_json(row["binding"])
            self._authorize(principal, binding, "executor")
            if row["status"] not in expected or status not in allowed.get(row["status"], set()):
                raise AccessDenied("Invalid operation transition")
            if status == "SUBMITTED":
                approval = db.execute(
                    "SELECT expires FROM service_approvals WHERE id=%s", (row["approval_id"],)
                ).fetchone()
                if approval["expires"] <= int(datetime.now(UTC).timestamp()):
                    raise AccessDenied("Approval expired before submission")
            if status in {"SUCCEEDED", "NO_EFFECT"} and (
                not receipt
                or receipt.get("binding_digest") != digest(binding)
                or receipt.get("verified") is not True
            ):
                raise AccessDenied("Bound verifier receipt required")
            db.execute(
                "UPDATE service_operations SET status=%s,receipt=%s WHERE id=%s",
                (status, json.dumps(receipt) if receipt else None, operation),
            )
            if status in {"SUCCEEDED", "NO_EFFECT"}:
                db.execute("DELETE FROM service_locks WHERE operation=%s", (operation,))

    def reserve_model_call(self, principal, scope, limit=2):
        self.store._authorized(principal, "assessor")
        if scope.tenant_id != self.store.tenant_id or not 1 <= limit <= 5:
            raise AccessDenied("Invalid model budget scope")
        key = str(scope.run_id)
        with self.store.transaction() as db:
            db.execute("INSERT INTO service_budgets VALUES(%s,0) ON CONFLICT DO NOTHING", (key,))
            row = db.execute(
                "UPDATE service_budgets SET used=used+1 WHERE id=%s AND used<%s RETURNING used",
                (key, limit),
            ).fetchone()
            if not row:
                raise AccessDenied("Durable model budget exhausted")
