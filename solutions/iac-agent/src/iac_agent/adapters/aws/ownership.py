"""Classifies every discovered AWS resource: ours, another tool's, AWS's, a default, or already in state.

Only "ours" on a certified type can be adopted. Ownership flows from parents:
- Anything inside a resource another tool or AWS owns inherits that owner (a
  subnet in a CloudFormation VPC, a rule on a CloudFormation security group).
- Structural children (rules, routes, associations, the default VPC's gateway)
  also inherit from default or already-in-state parents: they cannot be managed
  apart from their parent without fighting it.
- Other hand-built resources inside a default VPC (a subnet, SG or instance)
  stay adoptable; they reference the default VPC by its ID.
When in doubt, a resource is excluded: blocked beats guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...core.models import Classification, Discovery, Ownership, Resource, Tier
from . import import_ids as ids

Own = Ownership

# Tag prefixes that mean another tool created and manages the resource.
OTHER_TOOL_TAGS = {
    "aws:cloudformation:": "CloudFormation",
    "aws:autoscaling:": "Auto Scaling",
    "aws:servicecatalog:": "Service Catalog",
    "aws:eks:": "EKS",
    "eks:": "EKS",
    "kubernetes.io/cluster/": "EKS/Kubernetes",
    "elasticbeanstalk:": "Elastic Beanstalk",
}
FOREIGN = {Own.OTHER_TOOL, Own.CLOUD_MANAGED, Own.PART_OF_PARENT}
STRUCTURAL = {
    "aws_route",
    "aws_route_table_association",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
    "aws_internet_gateway",
}
# Discovered for the report, never adopted in V1 even when hand-built.
NOT_IN_V1 = {
    "aws_network_acl": "NACLs are V2",
    "aws_network_interface": "standalone ENIs are not in V1",
    "aws_launch_template": "launch templates are V2",
    "aws_autoscaling_group": "Auto Scaling is excluded in V1",
}


def state_keys(tfstate: Mapping[str, Any]) -> set[tuple[str, str]]:
    """(type, import ID) of every managed resource in a client's Terraform state file."""
    keys = set()
    for r in tfstate.get("resources", []):
        if r.get("mode") != "managed":
            continue
        for inst in r.get("instances", []):
            import_id = ids.from_state(r["type"], inst.get("attributes", {}))
            if import_id:
                keys.add((r["type"], import_id))
    return keys


def _own_signal(r: Resource, signals: Mapping[str, Any], in_state: set[tuple[str, str]]) -> tuple[Own, str]:
    a, t = r.attributes, r.terraform_type
    if r.key in in_state:
        return Own.IN_STATE, "already in a client-provided Terraform state"
    stacks = signals.get("cloudformation", {})
    for rid in [r.import_id, *a.get("native_ids", [])]:
        if rid in stacks:
            return Own.OTHER_TOOL, f"owned by CloudFormation stack {stacks[rid]}"
    for key, value in sorted(r.tags.items()):
        for prefix, tool in OTHER_TOOL_TAGS.items():
            if key.startswith(prefix):
                return Own.OTHER_TOOL, f"owned by {tool} (tag {key}={value})"
    if t == "aws_instance" and r.import_id in signals.get("autoscaling_instances", {}):
        return Own.OTHER_TOOL, f"launched by Auto Scaling group {signals['autoscaling_instances'][r.import_id]}"
    if t == "aws_autoscaling_group":
        return Own.OTHER_TOOL, "Auto Scaling is excluded in V1"
    defaults = {
        "aws_vpc": (a.get("is_default"), "default VPC"),
        "aws_subnet": (a.get("default_for_az"), "default subnet"),
        "aws_route_table": (a.get("main"), "AWS-created main route table"),
        "aws_security_group": (a.get("group_name") == "default", "AWS-created default security group"),
        "aws_network_acl": (a.get("is_default"), "AWS-created default network ACL"),
    }
    if t in defaults and defaults[t][0]:
        return Own.DEFAULT, defaults[t][1]
    if t == "aws_iam_role" and a.get("path", "").startswith("/aws-service-role/"):
        return Own.CLOUD_MANAGED, "service-linked role (/aws-service-role/)"
    if t == "aws_iam_role" and a.get("path", "").startswith("/aws-reserved/"):
        return Own.CLOUD_MANAGED, "AWS-reserved role (/aws-reserved/)"
    if t == "aws_iam_policy" and a.get("aws_managed"):
        return Own.CLOUD_MANAGED, "AWS-managed policy: referenced, never imported"
    if t == "aws_network_interface":
        if a.get("requester_managed") or a.get("interface_type", "interface") != "interface":
            return Own.CLOUD_MANAGED, f"service-created ENI ({a.get('interface_type')})"
        if a.get("device_index") == 0:
            return Own.PART_OF_PARENT, "primary ENI, managed through aws_instance"
    if t == "aws_ebs_volume" and a.get("root_of"):
        return Own.PART_OF_PARENT, "root volume, managed through aws_instance root_block_device"
    return Own.OURS, "hand-built"


def classify(discovery: Discovery, in_state: Iterable[tuple[str, str]] = ()) -> list[Classification]:
    in_state = set(in_state)
    by_key = {r.key: r for r in discovery.resources}
    own = {r.key: _own_signal(r, discovery.signals, in_state) for r in discovery.resources}
    final: dict[tuple[str, str], tuple[Own, str]] = {}

    def resolve(key: tuple[str, str], seen: frozenset = frozenset()) -> tuple[Own, str]:
        if key in final:
            return final[key]
        ownership, reason = own[key]
        if ownership is Own.OURS and key not in seen:
            for ptype, pid in by_key[key].attributes.get("parents", []):
                if (ptype, pid) not in by_key:
                    continue  # parent outside this region or not discovered
                p_own, p_reason = resolve((ptype, pid), seen | {key})
                if p_own in FOREIGN or (p_own is not Own.OURS and key[0] in STRUCTURAL):
                    ownership, reason = p_own, f"part of {ptype} {pid} ({p_reason})"
                    break
        final[key] = (ownership, reason)
        return final[key]

    result = []
    for r in discovery.resources:
        ownership, reason = resolve(r.key)
        if ownership is not Own.OURS:
            tier = Tier.EXCLUDED
        elif r.attributes.get("best_effort"):
            tier = Tier.BEST_EFFORT
        elif r.terraform_type in ids.CERTIFIED:
            tier = Tier.CERTIFIED
        else:
            tier, reason = Tier.EXCLUDED, f"not in V1 scope: {NOT_IN_V1.get(r.terraform_type, 'uncertified type')}"
        result.append(Classification(resource=r, ownership=ownership, tier=tier, reason=reason))
    return result
