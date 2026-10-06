"""Execution coordinator with immutable approval bindings and unknown-outcome reconciliation."""

from typing import Protocol

from .ledger import ExecutionBinding, LocalLedger
from .models import Principal, digest
from .tools import AccessDenied


class ExecutionAdapter(Protocol):
    version: str

    def observe(self, binding: ExecutionBinding) -> dict: ...
    def execute(self, binding: ExecutionBinding, operation: str) -> dict: ...
    def reconcile(self, binding: ExecutionBinding, operation: str) -> tuple[str, dict]: ...


class Executor:
    def __init__(
        self,
        ledger: LocalLedger,
        adapter: ExecutionAdapter,
        qualified_adapters: frozenset[str] = frozenset(),
    ):
        self.ledger, self.adapter, self.qualified_adapters = ledger, adapter, qualified_adapters

    def execute(self, principal: Principal, binding: ExecutionBinding, approval: str):
        self.ledger.authorize(principal, binding, "executor")
        if (
            self.adapter.version not in self.qualified_adapters
            or binding.adapter_version != self.adapter.version
        ):
            raise AccessDenied("Adapter lacks trusted qualification; execution remains disabled")
        observation = self.adapter.observe(binding)
        if (
            observation.get("inventory_digest") != binding.inventory_digest
            or observation.get("state_digest") != binding.destination_state_digest
            or observation.get("artifact_digest") != binding.artifact_digest
            or observation.get("plan_digest") != binding.plan_digest
            or observation.get("blockers") != []
        ):
            raise AccessDenied("Drift, changed artifacts or policy blockers invalidate approval")
        operation = self.ledger.begin(principal, binding, approval)
        # Repeat drift checks under resource locks; changed observations stop before submission.
        try:
            if self.adapter.observe(binding) != observation:
                raise AccessDenied("Observation changed after lock acquisition")
        except BaseException:
            self.ledger.transition(
                principal,
                operation,
                ("INTENT",),
                "NO_EFFECT",
                {
                    "binding_digest": digest(binding),
                    "verified": True,
                    "reason": "Execution was not submitted",
                },
            )
            raise
        # State is durable before effects. A worker crash leaves an unresolved locked operation.
        self.ledger.transition(principal, operation, ("INTENT",), "SUBMITTED")
        try:
            receipt = self.adapter.execute(binding, operation)
            if (
                receipt.get("binding_digest") != digest(binding)
                or receipt.get("verified") is not True
            ):
                raise AccessDenied("Execution result lacks bound verification")
        except BaseException:
            self.ledger.transition(principal, operation, ("SUBMITTED",), "OUTCOME_UNKNOWN")
            raise
        self.ledger.transition(principal, operation, ("SUBMITTED",), "SUCCEEDED", receipt)
        return operation

    def reconcile(self, principal: Principal, operation: str):
        record = self.ledger.read(principal, operation)
        binding = ExecutionBinding.model_validate_json(record["binding"])
        self.ledger.authorize(principal, binding, "executor")
        if binding.adapter_version != self.adapter.version:
            raise AccessDenied("Reconciliation adapter does not match the recorded operation")
        if record["status"] in {"INTENT", "SUBMITTED"}:
            self.ledger.transition(principal, operation, (record["status"],), "OUTCOME_UNKNOWN")
        elif record["status"] != "OUTCOME_UNKNOWN":
            raise AccessDenied("Operation does not require reconciliation")
        outcome, receipt = self.adapter.reconcile(binding, operation)
        if outcome not in {"SUCCEEDED", "NO_EFFECT"}:
            return "OUTCOME_UNKNOWN"
        if receipt.get("binding_digest") != digest(binding) or receipt.get("verified") is not True:
            raise AccessDenied("Reconciliation lacks bound verification")
        self.ledger.transition(principal, operation, ("OUTCOME_UNKNOWN",), outcome, receipt)
        return outcome
