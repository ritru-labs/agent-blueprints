"""Cloud-neutral data model shared by the pipeline, gates, adapters and reports."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Ownership(StrEnum):
    OURS = "ours"
    OTHER_TOOL = "other_tool"  # CloudFormation, Auto Scaling, EKS, Beanstalk, Service Catalog
    CLOUD_MANAGED = "cloud_managed"  # service-linked roles, service-created ENIs, managed policies
    DEFAULT = "default"  # default VPC and the defaults every VPC gets
    IN_STATE = "in_state"  # already owned by a client-provided Terraform state
    PART_OF_PARENT = "part_of_parent"  # managed through another resource (root volume, primary ENI)


class Tier(StrEnum):
    CERTIFIED = "certified"
    BEST_EFFORT = "best_effort"
    EXCLUDED = "excluded"


class Resource(BaseModel):
    """One live cloud object, as read by discovery. Never holds secret values."""

    model_config = ConfigDict(frozen=True)

    terraform_type: str
    import_id: str
    name: str | None = None  # Name tag, or the natural name (bucket, role)
    tags: dict[str, str] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)  # config used for ownership + fingerprint

    @property
    def key(self) -> tuple[str, str]:
        return (self.terraform_type, self.import_id)

    def fingerprint(self) -> str:
        """Hash of everything the generated code depends on; any change means re-scan."""
        # JSON-mode dump first, so a resource read fresh from the cloud and one reloaded from the
        # run checkpoint (dates already strings) hash the same.
        body = json.dumps(self.model_dump(mode="json", exclude={"name"}), sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


class Coverage(BaseModel):
    terraform_type: str
    complete: bool
    count: int = 0
    error: str | None = None  # e.g. "AccessDenied: ec2:DescribeVpcs"


class Discovery(BaseModel):
    account: str
    region: str
    resources: list[Resource] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)  # adapter-specific ownership inputs

    @property
    def incomplete(self) -> list[Coverage]:
        return [c for c in self.coverage if not c.complete]

    def fingerprints(self, keys: set[tuple[str, str]] | None = None) -> dict[str, str]:
        return {
            f"{r.terraform_type}:{r.import_id}": r.fingerprint()
            for r in self.resources
            if keys is None or r.key in keys
        }


class Classification(BaseModel):
    resource: Resource
    ownership: Ownership
    tier: Tier
    reason: str

    @property
    def adoptable(self) -> bool:
        return self.ownership is Ownership.OURS and self.tier is not Tier.EXCLUDED


class ScopeItem(BaseModel):
    """One resource a human approved for adoption, with its Terraform address."""

    model_config = ConfigDict(frozen=True)

    terraform_type: str
    import_id: str
    address: str  # e.g. aws_vpc.vpc_main


class GateOutcome(StrEnum):
    PASS = "pass"
    REPAIR = "repair"  # fixable in code; goes to the repair loop
    HARD_STOP = "hard_stop"  # replace or delete: human review, never auto-fixed
    BLOCKED = "blocked"  # cannot continue until a human fixes inputs (coverage, account)
    FAIL = "fail"  # agent bug or tool error; the run stops and reports


SEVERITY = [GateOutcome.PASS, GateOutcome.REPAIR, GateOutcome.FAIL, GateOutcome.BLOCKED, GateOutcome.HARD_STOP]


class Finding(BaseModel):
    outcome: GateOutcome
    message: str
    address: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)  # never holds sensitive values


class GateResult(BaseModel):
    gate: str
    findings: list[Finding] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def outcome(self) -> GateOutcome:
        return max((f.outcome for f in self.findings), key=SEVERITY.index, default=GateOutcome.PASS)

    @property
    def passed(self) -> bool:
        return self.outcome is GateOutcome.PASS

    def for_address(self, address: str) -> list[Finding]:
        return [f for f in self.findings if f.address == address]


# --- Fixture manifests (fixtures/*/create.sh) ---------------------------------


class ManifestResource(BaseModel):
    terraform_type: str
    import_id: str
    expect: Literal["adopt", "exclude"]
    reason: str


class ManifestExpect(BaseModel):
    outcome: Literal["pass", "blocked", "restart", "hard_stop"]
    step: str | None = None
    detail: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow")  # scanner_policy, drift, mutation

    fixture: str
    run: str
    account: str
    region: str
    expect: ManifestExpect
    resources: list[ManifestResource]

    def keys(self, expect: Literal["adopt", "exclude"]) -> set[tuple[str, str]]:
        return {(r.terraform_type, r.import_id) for r in self.resources if r.expect == expect}

    def expected_state(self) -> set[tuple[str, str]]:
        """What must be in state at the end: the adopt list, or nothing unless the run passes."""
        return self.keys("adopt") if self.expect.outcome in ("pass", "restart") else set()
