"""Fixed import ID formats per AWS type. Chosen in code, never by the LLM.

Source: "Import" section of each resource's doc in the AWS provider v6.67.0
(website/docs/r/<type>.html.markdown).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

CERTIFIED = frozenset({
    "aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_nat_gateway", "aws_eip",
    "aws_route_table", "aws_route", "aws_route_table_association",
    "aws_security_group", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
    "aws_s3_bucket", "aws_s3_bucket_versioning", "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_public_access_block", "aws_s3_bucket_policy", "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_ownership_controls",
    "aws_iam_role", "aws_iam_policy", "aws_iam_role_policy", "aws_iam_role_policy_attachment",
    "aws_iam_instance_profile",
    "aws_instance", "aws_ebs_volume", "aws_volume_attachment", "aws_key_pair",
})  # fmt: skip

_HEX = r"[0-9a-f]+"
_NAME = r"[\w+=,.@-]+"
_POLICY_ARN = r"arn:aws[\w-]*:iam::(aws|\d{12}):policy/[\w+=,.@/-]+"
_BUCKET = r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"
_CIDR4 = r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}"

PATTERNS: dict[str, str] = {
    "aws_vpc": rf"vpc-{_HEX}",
    "aws_subnet": rf"subnet-{_HEX}",
    "aws_internet_gateway": rf"igw-{_HEX}",
    "aws_nat_gateway": rf"nat-{_HEX}",
    "aws_eip": rf"eipalloc-{_HEX}",
    "aws_route_table": rf"rtb-{_HEX}",
    "aws_route": rf"rtb-{_HEX}_{_CIDR4}",
    "aws_route_table_association": rf"(subnet|igw|vgw)-{_HEX}/rtb-{_HEX}",
    "aws_security_group": rf"sg-{_HEX}",
    "aws_vpc_security_group_ingress_rule": rf"sgr-{_HEX}",
    "aws_vpc_security_group_egress_rule": rf"sgr-{_HEX}",
    "aws_instance": rf"i-{_HEX}",
    "aws_ebs_volume": rf"vol-{_HEX}",
    "aws_volume_attachment": rf"/dev/[a-z0-9]+:vol-{_HEX}:i-{_HEX}",
    "aws_key_pair": r"[\x20-\x7e]{1,255}",
    "aws_iam_role": _NAME,
    "aws_iam_instance_profile": _NAME,
    "aws_iam_policy": _POLICY_ARN,
    "aws_iam_role_policy": rf"{_NAME}:{_NAME}",
    "aws_iam_role_policy_attachment": rf"{_NAME}/{_POLICY_ARN}",
    # Discovered for the report only (never adopted in V1).
    "aws_network_acl": rf"acl-{_HEX}",
    "aws_network_interface": rf"eni-{_HEX}",
    "aws_launch_template": rf"lt-{_HEX}",
    "aws_autoscaling_group": r"[^:]{1,255}",
    **{t: _BUCKET for t in CERTIFIED if t.startswith("aws_s3_bucket")},
}

# Attributes that hold a resource's own ID in HCL (literals by nature).
IDENTITY_ATTRS: dict[str, set[str]] = {
    "aws_s3_bucket": {"bucket"},
    "aws_iam_role": {"name"},
    "aws_iam_policy": {"name"},
    "aws_iam_instance_profile": {"name"},
    "aws_key_pair": {"key_name"},
}

# Inline blocks that would mix styles with the separate resources we generate.
INLINE_BLOCKS: dict[str, set[str]] = {
    "aws_security_group": {"ingress", "egress"},
    "aws_route_table": {"route"},
    "aws_iam_role": {"inline_policy", "managed_policy_arns"},
    "aws_s3_bucket": {
        "versioning", "server_side_encryption_configuration", "lifecycle_rule", "policy", "acl",
        "logging", "website", "cors_rule", "replication_configuration", "object_lock_configuration",
    },
    "aws_instance": {"ebs_block_device"},
}  # fmt: skip


def route(route_table_id: str, destination_cidr: str) -> str:
    return f"{route_table_id}_{destination_cidr}"


def route_table_association(target_id: str, route_table_id: str) -> str:
    """target: a subnet ID or a gateway (igw-/vgw-) ID."""
    return f"{target_id}/{route_table_id}"


def volume_attachment(device: str, volume_id: str, instance_id: str) -> str:
    return f"{device}:{volume_id}:{instance_id}"


def role_policy(role: str, policy_name: str) -> str:
    return f"{role}:{policy_name}"


def role_policy_attachment(role: str, policy_arn: str) -> str:
    return f"{role}/{policy_arn}"


def is_valid(terraform_type: str, import_id: str) -> bool:
    pattern = PATTERNS.get(terraform_type)
    return bool(pattern and re.fullmatch(pattern, import_id))


def from_state(terraform_type: str, attrs: Mapping[str, Any]) -> str | None:
    """Import ID of a resource in a client's Terraform state, to detect double ownership."""
    a = attrs
    builders = {
        "aws_route": lambda: route(a["route_table_id"], a["destination_cidr_block"]),
        "aws_route_table_association": lambda: route_table_association(
            a.get("subnet_id") or a["gateway_id"], a["route_table_id"]
        ),
        "aws_volume_attachment": lambda: volume_attachment(a["device_name"], a["volume_id"], a["instance_id"]),
        "aws_iam_role_policy": lambda: role_policy(a["role"], a["name"]),
        "aws_iam_role_policy_attachment": lambda: role_policy_attachment(a["role"], a["policy_arn"]),
        "aws_iam_policy": lambda: a["arn"],
        "aws_eip": lambda: a.get("allocation_id") or a["id"],
    }
    try:
        return builders[terraform_type]() if terraform_type in builders else a.get("id")
    except KeyError:
        return a.get("id")
