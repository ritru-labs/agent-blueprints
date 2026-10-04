"""Real PostgreSQL service/ledger campaigns; no cloud or model endpoints are contacted."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_migration import Adapter, bundle_and_binding, configured_inventory
from test_service import AUDIENCE, ISSUER, access_token
from test_service import signing as signing

from infra_migration.execution import Executor
from infra_migration.models import Inventory, digest
from infra_migration.persistence import tenant_schema
from infra_migration.runner import DockerRunner
from infra_migration.service.api import create_app
from infra_migration.service.auth import JwtVerifier, actor_id
from infra_migration.service.runtime import PreparationWorker, Registry, TenantRuntime
from infra_migration.service.storage import PostgresLedger, TenantStore
from infra_migration.tools import AccessDenied

pytestmark = pytest.mark.skipif(
    not os.environ.get("INFRA_TEST_POSTGRES_DSN"), reason="Disposable PostgreSQL not configured"
)


@pytest.fixture
def stores():
    admin_dsn = os.environ["INFRA_TEST_POSTGRES_DSN"]
    tenants = [uuid4(), uuid4()]
    roles = ["service_" + uuid4().hex for _ in tenants]
    with psycopg.connect(admin_dsn, autocommit=True) as db:
        for role, tenant in zip(roles, tenants, strict=True):
            db.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'fixture-password'").format(
                    sql.Identifier(role)
                )
            )
            db.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(tenant_schema(tenant)), sql.Identifier(role)
                )
            )
    result = [
        TenantStore(make_conninfo(admin_dsn, user=r, password="fixture-password"), t)
        for r, t in zip(roles, tenants, strict=True)
    ]
    try:
        for store in result:
            store.setup()
            for subject, granted in (
                ("requester", ("assessor",)),
                ("reviewer", ("reviewer",)),
                ("worker", ("executor",)),
            ):
                store.provision_member(actor_id(ISSUER, subject), granted)
        yield result
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as db:
            for role, tenant in zip(roles, tenants, strict=True):
                db.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(tenant_schema(tenant))
                    )
                )
                db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def scoped_inventory(store):
    data = configured_inventory().model_dump(mode="json")
    data["scope"]["tenant_id"] = str(store.tenant_id)
    return Inventory.model_validate_json(json.dumps(data))


def test_authenticated_api_worker_restart_review_and_artifact_export(stores, signing, tmp_path):
    store, other = stores
    inventory = scoped_inventory(store)
    registry = Registry(
        {
            store.tenant_id: TenantRuntime(
                store,
                frozenset([inventory.scope.account_id]),
                frozenset(inventory.scope.regions),
                tmp_path / "a",
            ),
            other.tenant_id: TenantRuntime(
                other,
                frozenset(["999999999999"]),
                frozenset(inventory.scope.regions),
                tmp_path / "b",
            ),
        }
    )
    client = TestClient(create_app(JwtVerifier(ISSUER, AUDIENCE, signing[1]), registry))
    headers = {"Authorization": "Bearer " + access_token(signing[0])}
    base = f"/v1/organizations/{store.tenant_id}/runs"
    body = {
        "inventory": inventory.model_dump(mode="json"),
        "resource_ids": ["vpc-fixture", "subnet-fixture"],
        "idempotency_key": str(uuid4()),
    }
    submitted = client.post(base, json=body, headers=headers)
    assert submitted.status_code == 202, submitted.text
    run = submitted.json()["run_id"]
    assert client.post(base, json=body, headers=headers).json()["run_id"] == run
    changed = dict(body, resource_ids=["vpc-fixture"])
    assert client.post(base, json=changed, headers=headers).status_code == 403
    runner = (
        DockerRunner(os.environ["INFRA_RUNNER_IMAGE"])
        if os.environ.get("INFRA_RUNNER_IMAGE")
        else None
    )
    assert PreparationWorker(registry, runner=runner).once(store.tenant_id)
    result = client.get(base + "/" + run, headers=headers).json()
    assert result["status"] == "AWAITING_REVIEW", result
    assert result["result"]["input_provenance"] == "operator_supplied_snapshot"
    assert "UNVERIFIED_OPERATOR_SNAPSHOT" in result["result"]["blockers"]
    if runner:
        assert result["result"]["compile"]["passed"]
    review = {"plan_digest": result["result"]["review_digest"], "acknowledged": True}
    store.provision_member(actor_id(ISSUER, "requester"), ("assessor", "reviewer"))
    assert (
        client.post(base + "/" + run + "/review", json=review, headers=headers).status_code == 403
    )
    reviewer_headers = {"Authorization": "Bearer " + access_token(signing[0], "reviewer")}
    stale = dict(review, plan_digest="a" * 64)
    assert (
        client.post(base + "/" + run + "/review", json=stale, headers=reviewer_headers).status_code
        == 403
    )
    assert (
        client.post(base + "/" + run + "/review", json=review, headers=reviewer_headers).status_code
        == 202
    )
    assert PreparationWorker(registry, runner=runner).once(store.tenant_id)
    assert (
        client.get(base + "/" + run, headers=headers).json()["status"]
        == "REVIEWED_EXECUTION_BLOCKED"
    )
    archive = client.get(base + "/" + run + "/artifacts", headers=reviewer_headers)
    assert archive.status_code == 200 and archive.content.startswith(b"PK")
    assert archive.headers["cache-control"] == "no-store"
    other_url = f"/v1/organizations/{other.tenant_id}/runs/{run}"
    assert client.get(other_url, headers=headers).status_code == 403
    registry.directory(store.tenant_id, UUID(run)).joinpath("index.ts").write_text("tampered")
    assert client.get(base + "/" + run + "/artifacts", headers=headers).status_code == 403
    manifest = registry.directory(store.tenant_id, UUID(run)) / "bundle.json"
    manifest.write_text("customer-private-malformed-document")
    malformed = client.get(base + "/" + run + "/artifacts", headers=headers)
    assert malformed.status_code == 403 and "customer-private" not in malformed.text
    manifest.unlink()
    outside = tmp_path / "private.txt"
    outside.write_text("customer-private-outside-artifact-root")
    manifest.symlink_to(outside)
    assert client.get(base + "/" + run + "/artifacts", headers=headers).status_code == 403


def test_postgres_queue_claim_fencing_cancellation_and_membership_revocation(stores):
    store = stores[0]
    requester = store.principal(actor_id(ISSUER, "requester"))
    job = store.enqueue(requester, {"fixture": True}, uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(4)))
    claimed = [c for c in claims if c]
    assert len(claimed) == 1
    old = claimed[0]
    with store.transaction() as db:
        db.execute(
            "UPDATE service_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job["id"],),
        )
    new = store.claim()
    with pytest.raises(AccessDenied, match="superseded"):
        store.finish(job["id"], old["lease"], "FAILED", {})
    store.finish(job["id"], new["lease"], "FAILED", {"error": "Fixture"})
    queued = store.enqueue(requester, {"fixture": False}, uuid4())
    store.cancel(requester, queued["id"])
    assert store.get(requester, queued["id"])["status"] == "CANCELLED"
    store.provision_member(requester.subject, requester.roles, active=False)
    with pytest.raises(AccessDenied):
        store.get(requester, queued["id"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with stores[1].connection() as db:
            db.execute(
                sql.SQL("SELECT * FROM {}.service_jobs").format(
                    sql.Identifier(tenant_schema(store.tenant_id))
                )
            )


def ledger_fixture(store):
    _, _, binding = bundle_and_binding()
    binding = binding.model_copy(
        update={
            "scope": binding.scope.model_copy(update={"tenant_id": store.tenant_id}),
            "requester": actor_id(ISSUER, "requester"),
        }
    )
    reviewer = store.principal(actor_id(ISSUER, "reviewer"))
    worker = store.principal(actor_id(ISSUER, "worker"))
    return PostgresLedger(store), binding, reviewer, worker


def test_postgres_approval_atomic_consumption_and_cross_run_locks(stores):
    ledger, binding, reviewer, worker = ledger_fixture(stores[0])
    approval = ledger.approve(reviewer, binding)

    def begin(_):
        try:
            return ledger.begin(worker, binding, approval)
        except AccessDenied:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(begin, range(4)))
    operation = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    other_binding = binding.model_copy(
        update={"scope": binding.scope.model_copy(update={"run_id": uuid4()})}
    )
    other_approval = ledger.approve(reviewer, other_binding)
    with pytest.raises(AccessDenied, match="locked"):
        ledger.begin(worker, other_binding, other_approval)
    ledger.transition(worker, operation, ("INTENT",), "OUTCOME_UNKNOWN")
    ledger.transition(
        worker,
        operation,
        ("OUTCOME_UNKNOWN",),
        "NO_EFFECT",
        {"binding_digest": digest(binding), "verified": True},
    )
    assert ledger.begin(worker, other_binding, other_approval)
    with pytest.raises(AccessDenied):
        PostgresLedger(stores[1]).read(worker, operation)


def test_postgres_unknown_operation_restart_reconciliation_without_replay(stores):
    ledger, binding, reviewer, worker = ledger_fixture(stores[0])
    adapter = Adapter(binding, fail=True)
    executor = Executor(ledger, adapter, frozenset([adapter.version]))
    approval = ledger.approve(reviewer, binding)
    with pytest.raises(TimeoutError):
        executor.execute(worker, binding, approval)
    with stores[0].connection() as db:
        operation = db.execute("SELECT id FROM service_operations").fetchone()["id"]
    assert ledger.read(worker, operation)["status"] == "OUTCOME_UNKNOWN"
    restarted = Executor(PostgresLedger(stores[0]), adapter, frozenset([adapter.version]))
    assert restarted.reconcile(worker, operation) == "SUCCEEDED"
    assert adapter.writes == 1


def test_postgres_expiry_bound_receipts_budget_and_revoked_roles(stores):
    store = stores[0]
    ledger, binding, reviewer, worker = ledger_fixture(store)
    approval = ledger.approve(reviewer, binding)
    operation = ledger.begin(worker, binding, approval)
    with store.transaction() as db:
        db.execute("UPDATE service_approvals SET expires=0 WHERE id=%s", (approval,))
    with pytest.raises(AccessDenied, match="expired"):
        ledger.transition(worker, operation, ("INTENT",), "SUBMITTED")
    with pytest.raises(AccessDenied, match="receipt"):
        ledger.transition(worker, operation, ("INTENT",), "NO_EFFECT", {"verified": True})
    requester = store.principal(binding.requester)
    ledger.reserve_model_call(requester, binding.scope)
    PostgresLedger(store).reserve_model_call(requester, binding.scope)
    with pytest.raises(AccessDenied, match="budget"):
        ledger.reserve_model_call(requester, binding.scope)
    store.provision_member(worker.subject, ("assessor",))
    with pytest.raises(AccessDenied):
        ledger.read(worker, operation)


def test_dashboard_run_pagination_permissions_and_scoped_preview(stores, signing, tmp_path):
    store, other = stores
    inventory = scoped_inventory(store)
    registry = Registry(
        {
            store.tenant_id: TenantRuntime(
                store,
                frozenset([inventory.scope.account_id]),
                frozenset(inventory.scope.regions),
                tmp_path / "a",
            ),
            other.tenant_id: TenantRuntime(
                other,
                frozenset(["999999999999"]),
                frozenset(inventory.scope.regions),
                tmp_path / "b",
            ),
        }
    )
    client = TestClient(create_app(JwtVerifier(ISSUER, AUDIENCE, signing[1]), registry))
    requester = store.principal(actor_id(ISSUER, "requester"))
    payload = {
        "inventory": inventory.model_dump(mode="json"),
        "resource_ids": ["vpc-fixture", "subnet-fixture"],
    }
    for _ in range(4):
        store.enqueue(requester, payload, uuid4())
    base = f"/v1/organizations/{store.tenant_id}/runs"
    headers = {"Authorization": "Bearer " + access_token(signing[0])}
    first = client.get(base + "?limit=2", headers=headers).json()
    second = client.get(base + "?limit=2&before=" + first["next_cursor"], headers=headers).json()
    assert len(first["runs"]) == len(second["runs"]) == 2
    assert second["next_cursor"] is None
    assert not set(r["run_id"] for r in first["runs"]) & set(r["run_id"] for r in second["runs"])
    assert client.get(base + "?limit=51", headers=headers).status_code == 422
    assert client.get(base + "?before=" + str(uuid4()), headers=headers).status_code == 403
    PreparationWorker(registry).once(store.tenant_id)
    row = store.list_runs(requester, limit=10)[0]
    ready = next(r for r in row if r["status"] == "AWAITING_REVIEW")
    run = str(ready["id"])
    assert client.get(base + "/" + run, headers=headers).json()["can_review"] is False
    reviewer_headers = {"Authorization": "Bearer " + access_token(signing[0], "reviewer")}
    assert client.get(base + "/" + run, headers=reviewer_headers).json()["can_review"] is True
    response = client.get(base + "/" + run + "/artifacts/preview", headers=headers)
    assert response.status_code == 200
    assert "aws.ec2.Vpc" in response.json()["content"]
    assert "expected-inputs.json" in response.json()["files"]
    assert (
        client.get(
            base + "/" + run + "/artifacts/preview?file=../../private", headers=headers
        ).status_code
        == 403
    )
    assert client.get(base, headers=headers).json()["runs"][0]["result"] is None
    assert "resource_ids" not in next(
        r["result"] for r in client.get(base, headers=headers).json()["runs"] if r["result"]
    )
    assert (
        client.get(
            f"/v1/organizations/{other.tenant_id}/runs/{run}/artifacts/preview", headers=headers
        ).status_code
        == 403
    )
