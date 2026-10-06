"""Read-only discovery of the certified AWS types, plus the signals ownership needs.

Every list call is paginated to the end. Any error while reading a group of
types (AccessDenied, throttling past the retry budget, anything else) marks
all of that group's types incomplete: a list is complete or it is not used.
Only Describe/List/Get calls are made; object contents and secrets are never
read. Each resource records its parents, so ownership can flow down to it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ...core.models import Coverage, Discovery, Resource
from . import import_ids as ids

ClientFactory = Callable[[str], Any]
RETRIES = Config(retries={"mode": "standard", "max_attempts": 8})

S3_NOT_FOUND = {
    "NoSuchTagSet",
    "NoSuchBucketPolicy",
    "NoSuchLifecycleConfiguration",
    "ServerSideEncryptionConfigurationNotFoundError",
    "NoSuchPublicAccessBlockConfiguration",
    "OwnershipControlsNotFoundError",
}


def boto3_clients(session: boto3.Session, region: str) -> ClientFactory:
    return lambda service: session.client(service, region_name=region, config=RETRIES)


def _tags(items: Iterable[dict] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in items or []}


def _pages(client: Any, op: str, key: str, **kwargs: Any) -> Iterator[dict]:
    for page in client.get_paginator(op).paginate(**kwargs):
        yield from page.get(key, [])


def _res(
    terraform_type: str,
    import_id: str,
    *,
    name: str | None = None,
    tags: dict | None = None,
    parents: Iterable[tuple[str, str]] = (),
    **attributes: Any,
) -> Resource:
    tags = tags or {}
    attributes = {k: v for k, v in attributes.items() if v is not None}
    if parents:
        attributes["parents"] = [list(p) for p in parents]
    return Resource(terraform_type=terraform_type, import_id=import_id, name=name or tags.get("Name"),
                    tags=tags, attributes=attributes)  # fmt: skip


# --- Readers. Each returns resources for the types it is registered with. -----


def read_network(c: ClientFactory) -> list[Resource]:
    ec2, out = c("ec2"), []
    for v in _pages(ec2, "describe_vpcs", "Vpcs"):
        out.append(
            _res(
                "aws_vpc",
                v["VpcId"],
                tags=_tags(v.get("Tags")),
                cidr_block=v["CidrBlock"],
                is_default=v.get("IsDefault", False),
                instance_tenancy=v.get("InstanceTenancy"),
            )
        )
    for s in _pages(ec2, "describe_subnets", "Subnets"):
        out.append(
            _res(
                "aws_subnet",
                s["SubnetId"],
                tags=_tags(s.get("Tags")),
                parents=[("aws_vpc", s["VpcId"])],
                cidr_block=s["CidrBlock"],
                availability_zone=s["AvailabilityZone"],
                default_for_az=s.get("DefaultForAz", False),
                map_public_ip_on_launch=s.get("MapPublicIpOnLaunch", False),
            )
        )
    for g in _pages(ec2, "describe_internet_gateways", "InternetGateways"):
        vpcs = [a["VpcId"] for a in g.get("Attachments", [])]
        out.append(
            _res(
                "aws_internet_gateway",
                g["InternetGatewayId"],
                tags=_tags(g.get("Tags")),
                parents=[("aws_vpc", v) for v in vpcs],
                vpc_ids=vpcs,
            )
        )
    live = [{"Name": "state", "Values": ["pending", "available"]}]
    for n in _pages(ec2, "describe_nat_gateways", "NatGateways", Filter=live):
        out.append(
            _res(
                "aws_nat_gateway",
                n["NatGatewayId"],
                tags=_tags(n.get("Tags")),
                parents=[("aws_subnet", n["SubnetId"])],
                connectivity_type=n.get("ConnectivityType"),
                allocation_ids=[a["AllocationId"] for a in n.get("NatGatewayAddresses", []) if "AllocationId" in a],
            )
        )
    for a in ec2.describe_addresses(Filters=[{"Name": "domain", "Values": ["vpc"]}]).get("Addresses", []):
        out.append(
            _res(
                "aws_eip",
                a["AllocationId"],
                tags=_tags(a.get("Tags")),
                public_ip=a.get("PublicIp"),
                network_interface_id=a.get("NetworkInterfaceId"),
                instance_id=a.get("InstanceId"),
            )
        )
    for rt in _pages(ec2, "describe_route_tables", "RouteTables"):
        rtb = rt["RouteTableId"]
        main = any(x.get("Main") for x in rt.get("Associations", []))
        out.append(
            _res("aws_route_table", rtb, tags=_tags(rt.get("Tags")), parents=[("aws_vpc", rt["VpcId"])], main=main)
        )
        for r in rt.get("Routes", []):
            # Only routes someone created; "local" and propagated routes belong to the table.
            if r.get("Origin") == "CreateRoute" and "DestinationCidrBlock" in r:
                target = {k: v for k, v in r.items() if k.endswith("Id") and k != "DestinationPrefixListId"}
                out.append(
                    _res(
                        "aws_route",
                        ids.route(rtb, r["DestinationCidrBlock"]),
                        parents=[("aws_route_table", rtb)],
                        target=target,
                    )
                )
        for assoc in rt.get("Associations", []):
            target = assoc.get("SubnetId") or assoc.get("GatewayId")
            if assoc.get("Main") or not target:
                continue
            parent = ("aws_subnet", target) if target.startswith("subnet-") else ("aws_internet_gateway", target)
            out.append(
                _res(
                    "aws_route_table_association",
                    ids.route_table_association(target, rtb),
                    parents=[("aws_route_table", rtb), parent],
                    native_ids=[assoc["RouteTableAssociationId"]],
                )
            )
    for acl in _pages(ec2, "describe_network_acls", "NetworkAcls"):
        out.append(
            _res(
                "aws_network_acl",
                acl["NetworkAclId"],
                tags=_tags(acl.get("Tags")),
                parents=[("aws_vpc", acl["VpcId"])],
                is_default=acl.get("IsDefault", False),
            )
        )
    return out


def read_security_groups(c: ClientFactory) -> list[Resource]:
    ec2, out = c("ec2"), []
    for g in _pages(ec2, "describe_security_groups", "SecurityGroups"):
        out.append(
            _res(
                "aws_security_group",
                g["GroupId"],
                tags=_tags(g.get("Tags")),
                parents=[("aws_vpc", g["VpcId"])] if g.get("VpcId") else (),
                group_name=g["GroupName"],
                description=g.get("Description"),
            )
        )
    for r in _pages(ec2, "describe_security_group_rules", "SecurityGroupRules"):
        kind = "egress" if r["IsEgress"] else "ingress"
        out.append(
            _res(
                f"aws_vpc_security_group_{kind}_rule",
                r["SecurityGroupRuleId"],
                tags=_tags(r.get("Tags")),
                parents=[("aws_security_group", r["GroupId"])],
                ip_protocol=r.get("IpProtocol"),
                from_port=r.get("FromPort"),
                to_port=r.get("ToPort"),
                cidr_ipv4=r.get("CidrIpv4"),
                cidr_ipv6=r.get("CidrIpv6"),
                prefix_list_id=r.get("PrefixListId"),
                referenced_group_id=(r.get("ReferencedGroupInfo") or {}).get("GroupId"),
                description=r.get("Description"),
            )
        )
    return out


def read_compute(c: ClientFactory) -> list[Resource]:
    ec2, out = c("ec2"), []
    root_device: dict[str, str] = {}
    running = [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    for reservation in _pages(ec2, "describe_instances", "Reservations", Filters=running):
        for i in reservation.get("Instances", []):
            root_device[i["InstanceId"]] = i.get("RootDeviceName", "")
            out.append(
                _res(
                    "aws_instance",
                    i["InstanceId"],
                    tags=_tags(i.get("Tags")),
                    parents=[("aws_subnet", i["SubnetId"])] if i.get("SubnetId") else (),
                    instance_type=i.get("InstanceType"),
                    image_id=i.get("ImageId"),
                    availability_zone=(i.get("Placement") or {}).get("AvailabilityZone"),
                    iam_instance_profile=(i.get("IamInstanceProfile") or {}).get("Arn"),
                )
            )
    for v in _pages(ec2, "describe_volumes", "Volumes"):
        vol, root_of = v["VolumeId"], None
        for att in v.get("Attachments", []):
            inst, device = att["InstanceId"], att["Device"]
            if root_device.get(inst) == device:
                root_of = inst
            else:
                out.append(
                    _res(
                        "aws_volume_attachment",
                        ids.volume_attachment(device, vol, inst),
                        parents=[("aws_ebs_volume", vol), ("aws_instance", inst)],
                    )
                )
        out.append(
            _res(
                "aws_ebs_volume",
                vol,
                tags=_tags(v.get("Tags")),
                parents=[("aws_instance", root_of)] if root_of else (),
                root_of=root_of,
                availability_zone=v["AvailabilityZone"],
                size=v.get("Size"),
                volume_type=v.get("VolumeType"),
                encrypted=v.get("Encrypted"),
            )
        )
    for k in ec2.describe_key_pairs().get("KeyPairs", []):
        out.append(
            _res(
                "aws_key_pair",
                k["KeyName"],
                name=k["KeyName"],
                tags=_tags(k.get("Tags")),
                key_pair_id=k.get("KeyPairId"),
                key_type=k.get("KeyType"),
            )
        )
    for e in _pages(ec2, "describe_network_interfaces", "NetworkInterfaces"):
        att = e.get("Attachment") or {}
        out.append(
            _res(
                "aws_network_interface",
                e["NetworkInterfaceId"],
                tags=_tags(e.get("TagSet")),
                parents=[("aws_instance", att["InstanceId"])] if att.get("InstanceId") else (),
                interface_type=e.get("InterfaceType"),
                requester_managed=e.get("RequesterManaged", False),
                device_index=att.get("DeviceIndex"),
                description=e.get("Description"),
            )
        )
    for lt in _pages(ec2, "describe_launch_templates", "LaunchTemplates"):
        out.append(
            _res(
                "aws_launch_template",
                lt["LaunchTemplateId"],
                name=lt.get("LaunchTemplateName"),
                tags=_tags(lt.get("Tags")),
            )
        )
    return out


def _is_aws_owned_role(path: str) -> bool:
    return path.startswith(("/aws-service-role/", "/aws-reserved/"))


def read_iam(c: ClientFactory) -> list[Resource]:
    iam, out = c("iam"), []
    aws_managed: set[str] = set()
    for r in _pages(iam, "list_roles", "Roles"):
        role = r["RoleName"]
        aws_owned = _is_aws_owned_role(r["Path"])
        tags = {} if aws_owned else _tags(iam.list_role_tags(RoleName=role).get("Tags"))
        out.append(
            _res(
                "aws_iam_role",
                role,
                name=role,
                tags=tags,
                path=r["Path"],
                arn=r["Arn"],
                assume_role_policy=r.get("AssumeRolePolicyDocument"),
                max_session_duration=r.get("MaxSessionDuration"),
                description=r.get("Description"),
            )
        )
        if aws_owned:
            continue  # AWS owns these and everything attached to them
        for name in _pages(iam, "list_role_policies", "PolicyNames", RoleName=role):
            out.append(_res("aws_iam_role_policy", ids.role_policy(role, name), parents=[("aws_iam_role", role)]))
        for p in _pages(iam, "list_attached_role_policies", "AttachedPolicies", RoleName=role):
            arn = p["PolicyArn"]
            out.append(
                _res(
                    "aws_iam_role_policy_attachment",
                    ids.role_policy_attachment(role, arn),
                    parents=[("aws_iam_role", role)],
                    policy_arn=arn,
                )
            )
            if ":iam::aws:policy/" in arn:
                aws_managed.add(arn)
    for p in _pages(iam, "list_policies", "Policies", Scope="Local"):
        tags = _tags(iam.list_policy_tags(PolicyArn=p["Arn"]).get("Tags"))
        out.append(
            _res(
                "aws_iam_policy",
                p["Arn"],
                name=p["PolicyName"],
                tags=tags,
                path=p.get("Path"),
                default_version_id=p.get("DefaultVersionId"),
            )
        )
    for arn in sorted(aws_managed):
        out.append(_res("aws_iam_policy", arn, name=arn.rsplit("/", 1)[-1], aws_managed=True))
    for ip in _pages(iam, "list_instance_profiles", "InstanceProfiles"):
        name = ip["InstanceProfileName"]
        roles = [r["RoleName"] for r in ip.get("Roles", [])]
        tags = _tags(iam.list_instance_profile_tags(InstanceProfileName=name).get("Tags"))
        out.append(
            _res(
                "aws_iam_instance_profile",
                name,
                name=name,
                tags=tags,
                path=ip.get("Path"),
                parents=[("aws_iam_role", r) for r in roles],
                roles=roles,
            )
        )
    return out


def _s3_get(s3: Any, op: str, bucket: str) -> dict | None:
    try:
        return getattr(s3, op)(Bucket=bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] in S3_NOT_FOUND:
            return None
        raise


def read_s3(c: ClientFactory, region: str) -> list[Resource]:
    s3, out = c("s3"), []
    for b in _pages(s3, "list_buckets", "Buckets", BucketRegion=region):
        name = b["Name"]
        parent = [("aws_s3_bucket", name)]
        tags = _tags((_s3_get(s3, "get_bucket_tagging", name) or {}).get("TagSet"))
        out.append(_res("aws_s3_bucket", name, name=name, tags=tags, region=region))
        versioning = _s3_get(s3, "get_bucket_versioning", name) or {}
        if versioning.get("Status"):  # never enabled: nothing to adopt
            out.append(
                _res(
                    "aws_s3_bucket_versioning",
                    name,
                    name=name,
                    parents=parent,
                    status=versioning["Status"],
                    mfa_delete=versioning.get("MFADelete"),
                )
            )
        configs = [
            ("aws_s3_bucket_server_side_encryption_configuration", "get_bucket_encryption",
             lambda r: r["ServerSideEncryptionConfiguration"]),
            ("aws_s3_bucket_public_access_block", "get_public_access_block",
             lambda r: r["PublicAccessBlockConfiguration"]),
            ("aws_s3_bucket_policy", "get_bucket_policy", lambda r: json.loads(r["Policy"])),
            ("aws_s3_bucket_lifecycle_configuration", "get_bucket_lifecycle_configuration", lambda r: r["Rules"]),
            ("aws_s3_bucket_ownership_controls", "get_bucket_ownership_controls", lambda r: r["OwnershipControls"]),
        ]  # fmt: skip
        for terraform_type, op, pick in configs:
            resp = _s3_get(s3, op, name)
            if resp is not None:
                out.append(_res(terraform_type, name, name=name, parents=parent, config=pick(resp)))
    return out


def read_signals(c: ClientFactory) -> dict[str, Any]:
    """Who else owns what: CloudFormation physical IDs and Auto Scaling instances."""
    cfn, asg = c("cloudformation"), c("autoscaling")
    stacks: dict[str, str] = {}
    live = [s for s in _pages(cfn, "list_stacks", "StackSummaries") if s["StackStatus"] != "DELETE_COMPLETE"]
    for s in live:
        for r in _pages(cfn, "list_stack_resources", "StackResourceSummaries", StackName=s["StackName"]):
            if r.get("PhysicalResourceId"):
                stacks[r["PhysicalResourceId"]] = s["StackName"]
    groups: dict[str, str] = {}
    asg_names: list[str] = []
    for g in _pages(asg, "describe_auto_scaling_groups", "AutoScalingGroups"):
        asg_names.append(g["AutoScalingGroupName"])
        for i in g.get("Instances", []):
            groups[i["InstanceId"]] = g["AutoScalingGroupName"]
    return {"cloudformation": stacks, "autoscaling_instances": groups, "autoscaling_groups": asg_names}


NETWORK = ["aws_vpc", "aws_subnet", "aws_internet_gateway", "aws_nat_gateway", "aws_eip", "aws_route_table",
           "aws_route", "aws_route_table_association", "aws_network_acl"]  # fmt: skip
SECURITY = ["aws_security_group", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"]
COMPUTE = ["aws_instance", "aws_ebs_volume", "aws_volume_attachment", "aws_key_pair", "aws_network_interface",
           "aws_launch_template"]  # fmt: skip
IAM = ["aws_iam_role", "aws_iam_role_policy", "aws_iam_role_policy_attachment", "aws_iam_policy",
       "aws_iam_instance_profile"]  # fmt: skip
S3 = sorted(t for t in ids.CERTIFIED if t.startswith("aws_s3_bucket"))
SIGNALS = ["signal:cloudformation", "signal:autoscaling"]


def _error(e: Exception) -> str:
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        return f"{err.get('Code')}: {e.operation_name}"
    return type(e).__name__


def discover(clients: ClientFactory, account: str, region: str) -> Discovery:
    result = Discovery(account=account, region=region)
    groups: list[tuple[list[str], Callable[[], list[Resource]]]] = [
        (NETWORK, lambda: read_network(clients)),
        (SECURITY, lambda: read_security_groups(clients)),
        (COMPUTE, lambda: read_compute(clients)),
        (IAM, lambda: read_iam(clients)),
        (S3, lambda: read_s3(clients, region)),
    ]
    for types, read in groups:
        try:
            found = read()
        except (ClientError, BotoCoreError) as e:
            result.coverage += [Coverage(terraform_type=t, complete=False, error=_error(e)) for t in types]
            continue
        result.resources += found
        result.coverage += [
            Coverage(terraform_type=t, complete=True, count=sum(r.terraform_type == t for r in found)) for t in types
        ]
    try:
        result.signals = read_signals(clients)
        result.coverage += [Coverage(terraform_type=t, complete=True) for t in SIGNALS]
    except (ClientError, BotoCoreError) as e:
        # Without ownership signals nothing can be classified safely.
        result.coverage += [Coverage(terraform_type=t, complete=False, error=_error(e)) for t in SIGNALS]
    return result
