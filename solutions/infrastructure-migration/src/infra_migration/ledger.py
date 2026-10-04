"""Transactional local approval and operation ledger; production storage requires qualification."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .models import Contract, Principal, Scope, digest
from .tools import AccessDenied


class ExecutionBinding(Contract):
    scope: Scope
    action: Literal["import", "source_retain", "source_release"]
    requester: str = Field(min_length=1)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: str = Field(
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9_-]*(/[A-Za-z0-9_][A-Za-z0-9_-]*){0,2}$"
    )
    resources: tuple[str, ...] = Field(min_length=1, max_length=1000)
    adapter_version: str = Field(min_length=1)
    policy_version: Literal["adoption-only-v1"] = "adoption-only-v1"

    @model_validator(mode="after")
    def bounded_batch(self):
        if len(self.scope.regions) != 1 or len(set(self.resources)) != len(self.resources):
            raise ValueError("Execution batches require one region and unique resources")
        return self


class LocalLedger:
    def __init__(self, path: Path):
        if path.is_symlink():
            raise AccessDenied("Ledger path must not be a symlink")
        self.path = path
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, binding TEXT NOT NULL,
                approver TEXT NOT NULL, expires INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, binding TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT, approval_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS locks (resource TEXT PRIMARY KEY, operation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (id TEXT PRIMARY KEY, used INTEGER NOT NULL);
            """)
        path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def authorize(principal, binding, role):
        if principal.tenant_id != binding.scope.tenant_id or role not in principal.roles:
            raise AccessDenied("Ledger authorization failed")

    def approve(self, principal: Principal, binding: ExecutionBinding, *, ttl=300, now=None):
        self.authorize(principal, binding, "reviewer")
        if principal.subject == binding.requester:
            raise AccessDenied("Requester cannot approve their own execution")
        if not 1 <= ttl <= 3600:
            raise ValueError("Approval TTL outside policy")
        now = int(datetime.now(UTC).timestamp()) if now is None else now
        approval = str(uuid4())
        with self.transaction() as db:
            db.execute(
                "INSERT INTO approvals(id,tenant,binding,approver,expires) VALUES(?,?,?,?,?)",
                (approval, str(principal.tenant_id), digest(binding), principal.subject, now + ttl),
            )
        return approval

    def reserve_model_call(self, principal: Principal, scope: Scope, limit=2):
        if principal.tenant_id != scope.tenant_id or "assessor" not in principal.roles:
            raise AccessDenied("Model budget authorization failed")
        key = f"model:{scope.tenant_id}:{scope.run_id}"
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO budgets VALUES(?,0)", (key,))
            used = db.execute("SELECT used FROM budgets WHERE id=?", (key,)).fetchone()["used"]
            if used >= limit:
                raise AccessDenied("Durable model budget exhausted")
            db.execute("UPDATE budgets SET used=used+1 WHERE id=?", (key,))

    def begin(self, principal: Principal, binding: ExecutionBinding, approval: str, *, now=None):
        self.authorize(principal, binding, "executor")
        now = int(datetime.now(UTC).timestamp()) if now is None else now
        operation = str(uuid4())
        with self.transaction() as db:
            record = db.execute("SELECT * FROM approvals WHERE id=?", (approval,)).fetchone()
            if (
                not record
                or record["tenant"] != str(principal.tenant_id)
                or record["binding"] != digest(binding)
                or record["expires"] <= now
                or record["consumed"]
                or record["approver"] == principal.subject
            ):
                raise AccessDenied("Approval expired, consumed, substituted or unauthorized")
            try:
                for resource in sorted(set(binding.resources)):
                    # Cloud resource locks span run IDs to prevent overlapping migrations.
                    key = digest(
                        {
                            "cloud": binding.scope.cloud,
                            "account": binding.scope.account_id,
                            "regions": sorted(binding.scope.regions),
                            "resource": resource,
                        }
                    )
                    db.execute("INSERT INTO locks VALUES(?,?)", (key, operation))
            except sqlite3.IntegrityError:
                raise AccessDenied("Resource is locked by an unresolved operation") from None
            db.execute("UPDATE approvals SET consumed=1 WHERE id=?", (approval,))
            db.execute(
                "INSERT INTO operations(id,tenant,binding,status,approval_id) VALUES(?,?,?,?,?)",
                (
                    operation,
                    str(principal.tenant_id),
                    binding.model_dump_json(),
                    "INTENT",
                    approval,
                ),
            )
        return operation

    def read(self, principal: Principal, operation: str):
        if not any(role in principal.roles for role in ("reviewer", "executor")):
            raise AccessDenied("Operation read role required")
        with self.connection() as db:
            record = db.execute(
                "SELECT * FROM operations WHERE id=? AND tenant=?",
                (operation, str(principal.tenant_id)),
            ).fetchone()
        if not record:
            raise AccessDenied("Operation is unavailable in this tenant")
        return dict(record)

    def transition(
        self,
        principal: Principal,
        operation: str,
        expected: tuple[str, ...],
        status: Literal["SUBMITTED", "OUTCOME_UNKNOWN", "SUCCEEDED", "NO_EFFECT"],
        receipt: dict | None = None,
    ):
        allowed = {
            "INTENT": {"SUBMITTED", "OUTCOME_UNKNOWN", "NO_EFFECT"},
            "SUBMITTED": {"OUTCOME_UNKNOWN", "SUCCEEDED"},
            "OUTCOME_UNKNOWN": {"SUCCEEDED", "NO_EFFECT"},
        }
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=? AND tenant=?",
                (operation, str(principal.tenant_id)),
            ).fetchone()
            if not row:
                raise AccessDenied("Operation unavailable")
            binding = ExecutionBinding.model_validate_json(row["binding"])
            self.authorize(principal, binding, "executor")
            if status == "SUBMITTED":
                approval = db.execute(
                    "SELECT expires FROM approvals WHERE id=?", (row["approval_id"],)
                ).fetchone()
                if not approval or approval["expires"] <= int(datetime.now(UTC).timestamp()):
                    raise AccessDenied("Approval expired before operation submission")
            if row["status"] not in expected or status not in allowed.get(row["status"], set()):
                raise AccessDenied("Invalid operation state transition")
            if status in {"SUCCEEDED", "NO_EFFECT"} and not receipt:
                raise AccessDenied("Terminal operation requires verifier receipt")
            db.execute(
                "UPDATE operations SET status=?,receipt=? WHERE id=?",
                (status, json.dumps(receipt) if receipt else None, operation),
            )
            if status in {"SUCCEEDED", "NO_EFFECT"}:
                db.execute("DELETE FROM locks WHERE operation=?", (operation,))
