"""Application-side allowlist. No shell, credential, network or cloud write tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .models import Contract, Inventory, Principal, Scope, digest


class AccessDenied(RuntimeError):
    pass


class ToolRequest(Contract):
    name: Literal["read_inventory", "import_resources", "apply", "release_ownership"]
    scope: Scope
    request_id: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class ToolReceipt:
    request_id: str
    tool: str
    result_digest: str
    provenance: str


class FixtureGateway:
    """Single-run synthetic adapter; fixtures cannot enable privileged tools."""

    def __init__(self, inventory: Inventory):
        if inventory.provenance != "synthetic_fixture":
            raise AccessDenied("Fixture gateway cannot impersonate a live cloud adapter")
        # Capture bytes so caller mutations cannot silently change the inventory.
        self._inventory = inventory.model_dump_json()

    def invoke(self, principal: Principal, request: ToolRequest) -> tuple[Inventory, ToolReceipt]:
        if principal.tenant_id != request.scope.tenant_id or "assessor" not in principal.roles:
            raise AccessDenied("Assessor role and matching tenant required")
        if request.name != "read_inventory":
            raise AccessDenied("Live execution is disabled in this release")
        inventory = Inventory.model_validate_json(self._inventory)
        if inventory.scope != request.scope:
            raise AccessDenied("Inventory does not belong to this exact migration scope")
        if len(self._inventory.encode()) > 4_000_000:
            raise AccessDenied("Inventory exceeds tool output budget")
        return inventory, ToolReceipt(
            request.request_id, request.name, digest(inventory), inventory.provenance
        )
