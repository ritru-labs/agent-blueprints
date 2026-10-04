"""Deterministic candidate assessment. Mapping candidates are not qualified adapters."""

from .models import AssessmentPlan, Inventory, ResourceAssessment, digest

CANDIDATES = {
    "AWS::EC2::VPC": "aws:ec2/vpc:Vpc",
    "AWS::EC2::Subnet": "aws:ec2/subnet:Subnet",
    "AWS::EC2::SecurityGroup": "aws:ec2/securityGroup:SecurityGroup",
    "AWS::S3::Bucket": "aws:s3/bucket:Bucket",
}


def assess(inventory: Inventory) -> AssessmentPlan:
    known = {resource.resource_id for resource in inventory.resources}
    dependencies = {r.resource_id: set(r.dependencies) & known for r in inventory.resources}
    # Kahn traversal catches cycles without recursion over potentially large inventories.
    indegree = {key: len(value) for key, value in dependencies.items()}
    children: dict[str, list[str]] = {key: [] for key in known}
    for key, deps in dependencies.items():
        for dep in deps:
            children[dep].append(key)
    ready = [key for key, count in indegree.items() if count == 0]
    visited = set()
    while ready:
        key = ready.pop()
        visited.add(key)
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    blocked_by_cycle = known - visited
    assessments = []
    blockers = ["LIVE_ADAPTERS_NOT_QUALIFIED", "CONFIGURATION_PARITY_NOT_VERIFIED"]
    if inventory.coverage != "complete_fixture" or inventory.gaps:
        blockers.append("DISCOVERY_INCOMPLETE")
    if not inventory.resources:
        blockers.append("EMPTY_INVENTORY_REQUIRES_REVIEW")
    for resource in sorted(inventory.resources, key=lambda r: r.resource_id):
        reasons = []
        target = CANDIDATES.get(resource.resource_type)
        if target is None:
            reasons.append("UNSUPPORTED_RESOURCE_TYPE")
        if resource.owner == "unknown":
            reasons.append("OWNERSHIP_UNKNOWN")
        expected_owner = (
            "manual" if inventory.scope.workflow == "manual_adoption" else "cloudformation"
        )
        if resource.owner != expected_owner:
            reasons.append("OWNERSHIP_TRANSFER_REQUIRES_REVIEW")
        if any(dep not in known for dep in resource.dependencies):
            reasons.append("UNRESOLVED_DEPENDENCY")
        if resource.resource_id in blocked_by_cycle:
            reasons.append("DEPENDENCY_CYCLE_OR_DEPENDENT")
        assessments.append(
            ResourceAssessment(
                resource_id=resource.resource_id,
                status="blocked" if reasons else "mapping_candidate",
                target_type=target,
                blockers=tuple(reasons),
            )
        )
    return AssessmentPlan(
        scope=inventory.scope,
        inventory_digest=digest(inventory),
        assessments=tuple(assessments),
        blockers=tuple(blockers),
    )
