"""Trusted tenant registry and durable, preparation-only LangGraph worker."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ..generation import ProjectBundle, verify_bundle_directory
from ..models import Inventory, ReviewDecision
from ..persistence import postgres_checkpointer
from ..pipeline import MigrationPipeline
from ..specialists import check_handoffs
from ..tools import AccessDenied
from .storage import PostgresLedger, TenantStore


@dataclass(frozen=True)
class TenantRuntime:
    store: TenantStore
    accounts: frozenset[str]
    regions: frozenset[str]
    artifact_root: Path


class Registry:
    def __init__(self, tenants: dict[UUID, TenantRuntime]):
        owned = set()
        for tenant, runtime in tenants.items():
            if tenant != runtime.store.tenant_id or not runtime.accounts or not runtime.regions:
                raise ValueError("Invalid tenant provisioning")
            if owned & runtime.accounts:
                raise ValueError("A cloud account cannot belong to multiple organizations")
            owned.update(runtime.accounts)
            root = runtime.artifact_root
            if root.is_symlink():
                raise ValueError("Artifact root must not be a symlink")
        roots = [r.artifact_root.resolve() for r in tenants.values()]
        for i, left in enumerate(roots):
            for right in roots[i + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise ValueError("Tenant artifact roots must be disjoint")
        for tenant, runtime in tenants.items():
            root = runtime.artifact_root
            marker = root / ".infra-migration-tenant"
            if root.exists():
                if not root.is_dir() or (not marker.exists() and any(root.iterdir())):
                    raise ValueError("Use a dedicated empty or previously owned artifact directory")
                if marker.is_symlink() or (marker.exists() and marker.read_text() != str(tenant)):
                    raise ValueError("Artifact directory belongs to another tenant")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not marker.exists():
                with marker.open("x") as file:
                    file.write(str(tenant))
            root.chmod(0o700)
        self.tenants = dict(tenants)

    def get(self, tenant):
        if tenant not in self.tenants:
            raise AccessDenied("Organization unavailable")
        return self.tenants[tenant]

    def directory(self, tenant, run):
        return self.get(tenant).artifact_root / str(UUID(str(run)))


class PreparationWorker:
    def __init__(self, registry: Registry, *, runner=None, model_factory=None, documents=()):
        self.registry, self.runner = registry, runner
        self.model_factory, self.documents = model_factory, documents

    def once(self, tenant: UUID):
        runtime = self.registry.get(tenant)
        store = runtime.store
        job = store.claim(lease_seconds=600)
        if job is None:
            return False
        try:
            with store.run_lock(job["id"]):
                self._process(runtime, job)
        except AccessDenied:
            # A stale worker must not change state owned by a new lease. A still-held
            # run lock is released by the prior process; retry remains budgeted.
            raise
        return True

    def _process(self, runtime, job):
        store, directory = runtime.store, self.registry.directory(store_id(runtime), job["id"])
        try:
            principal = store.principal(job["requester"])
            if "assessor" not in principal.roles:
                raise AccessDenied("Requester no longer has the required organization role")
            inventory = Inventory.model_validate_json(json.dumps(job["payload"]["inventory"]))
            data = inventory.model_dump(mode="json")
            data["scope"]["run_id"] = str(job["id"])
            data["provenance"] = "operator_supplied_snapshot"
            data["coverage"] = "partial"
            data["gaps"] = sorted(set(data["gaps"]) | {"UNVERIFIED_OPERATOR_SNAPSHOT"})
            inventory = Inventory.model_validate_json(json.dumps(data))
            if (
                inventory.scope.tenant_id != store.tenant_id
                or inventory.scope.account_id not in runtime.accounts
                or not set(inventory.scope.regions) <= runtime.regions
            ):
                raise AccessDenied("Prepared inventory is outside the provisioned scope")
            model = self.model_factory() if self.model_factory else None
            if model is not None:
                ledger = PostgresLedger(store)
                model.reserve_call = lambda: ledger.reserve_model_call(principal, inventory.scope)
            with postgres_checkpointer(store.dsn, store.tenant_id) as saver:
                pipeline = MigrationPipeline(
                    principal,
                    inventory.scope,
                    lambda: inventory,
                    saver,
                    runner=self.runner,
                    model_reviewer=model,
                    documents=self.documents,
                    output_directory=directory,
                )
                state = pipeline._snapshot()
                if not state.values:
                    pipeline.start(tuple(job["payload"]["resource_ids"]))
                elif state.next and state.next != ("review",):
                    # Only pure preparation nodes may be replayed. This graph has no writes.
                    pipeline.graph.invoke(None, pipeline.config)
                state = pipeline._snapshot()
                if job["review"]:
                    reviewer = store.principal(job["review"]["actor"])
                    if reviewer.subject == principal.subject:
                        raise AccessDenied("Requester cannot review their own preparation")
                    if state.next == ("review",):
                        pipeline.resume_review(
                            reviewer,
                            ReviewDecision.model_validate_json(
                                json.dumps(job["review"]["decision"])
                            ),
                        )
                    elif state.values.get("status") not in {
                        "REVIEWED_EXECUTION_BLOCKED",
                        "REJECTED",
                    }:
                        raise AccessDenied("Checkpoint is not ready for the queued review")
                    state = pipeline._snapshot()
                bundle = ProjectBundle.model_validate_json(state.values["bundle_json"])
                verify_bundle_directory(bundle, directory)
                pending = [i.value for task in state.tasks for i in task.interrupts]
                result = {
                    "artifact_digest": bundle.artifact_digest,
                    "resource_ids": list(bundle.resource_ids),
                    "blockers": list(bundle.blockers),
                    "compile": state.values["compile_receipt"],
                    "model": state.values["model_receipt"],
                    "review_digest": pending[0]["plan_digest"]
                    if pending
                    else state.values.get("package_digest"),
                    "input_provenance": "operator_supplied_snapshot",
                    "execution_enabled": False,
                }
                if state.values.get("architecture_version"):
                    handoffs = check_handoffs(state.values, inventory.scope, complete=True)
                    result["specialists"] = {
                        "architecture": state.values["architecture_version"],
                        "completed": [r.specialist for r in handoffs],
                        "handoffs": [r.model_dump(mode="json") for r in handoffs],
                    }
                    result["migration_plan"] = json.loads(state.values["migration_plan_json"])
                    result["blockers"] = sorted(
                        set(result["blockers"]) | set(result["migration_plan"]["blockers"])
                    )
                    result["recovery_plan"] = json.loads(state.values["recovery_plan_json"])
                status = "AWAITING_REVIEW" if pending else state.values["status"]
            store.finish(job["id"], job["lease"], status, result)
        except Exception as exc:
            # Never persist raw provider, input, database, or token exception text.
            store.finish(job["id"], job["lease"], "FAILED", {"error": type(exc).__name__})


def store_id(runtime):
    return runtime.store.tenant_id
