"""Strict contracts shared by the graph and the application-side tool gateway."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Scope(Contract):
    tenant_id: UUID
    run_id: UUID
    cloud: Literal["aws"] = "aws"
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    regions: tuple[str, ...] = Field(min_length=1, max_length=32)
    workflow: Literal["manual_adoption", "cloudformation_migration"]
    target: Literal["pulumi_typescript"] = "pulumi_typescript"

    @model_validator(mode="after")
    def unique_regions(self):
        if len(set(self.regions)) != len(self.regions) or any(not r.strip() for r in self.regions):
            raise ValueError("Regions must be unique and nonempty")
        return self


class Principal(Contract):
    """Supplied by a trusted authentication layer, never by model output."""

    tenant_id: UUID
    subject: str = Field(min_length=1)
    roles: tuple[Literal["assessor", "reviewer"], ...]


class Resource(Contract):
    resource_id: str = Field(min_length=1, max_length=512)
    resource_type: str = Field(min_length=1, max_length=128)
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(min_length=1)
    owner: Literal["manual", "cloudformation", "unknown"]
    dependencies: tuple[str, ...] = ()


class Inventory(Contract):
    scope: Scope
    provenance: Literal["synthetic_fixture"]
    observed_at: datetime
    coverage: Literal["complete_fixture", "partial"]
    gaps: tuple[str, ...] = ()
    resources: tuple[Resource, ...] = Field(max_length=10000)

    @model_validator(mode="after")
    def check_inventory(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("Inventory timestamp must include a timezone")
        ids = [r.resource_id for r in self.resources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate resource identities")
        for resource in self.resources:
            if resource.account_id != self.scope.account_id:
                raise ValueError("Resource account outside scope")
            if resource.region not in self.scope.regions:
                raise ValueError("Resource region outside scope")
        return self


class ResourceAssessment(Contract):
    resource_id: str
    status: Literal["mapping_candidate", "blocked"]
    target_type: str | None = None
    blockers: tuple[str, ...]


class AssessmentPlan(Contract):
    scope: Scope
    inventory_digest: str
    policy_version: Literal["assessment-v1"] = "assessment-v1"
    support_version: Literal["unqualified-candidates-v1"] = "unqualified-candidates-v1"
    assessments: tuple[ResourceAssessment, ...]
    blockers: tuple[str, ...]
    execution_enabled: Literal[False] = False


class ReviewDecision(Contract):
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledged: bool


def digest(value: BaseModel | dict) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
