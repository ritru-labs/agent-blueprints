"""Bounded AWS read adapter. No role creation, inventory setup or cloud mutation."""

from datetime import UTC, datetime
from time import monotonic

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .models import Inventory, Principal, Resource, Scope
from .tools import AccessDenied


class DiscoveryBudgetExceeded(RuntimeError):
    pass


class AwsDiscovery:
    def __init__(self, session=None, *, max_calls=200, max_resources=1000, timeout=120):
        if (
            not 1 <= max_calls <= 10000
            or not 1 <= max_resources <= 10000
            or not 1 <= timeout <= 600
        ):
            raise ValueError("Invalid discovery budget")
        self.session = session or boto3.Session()
        self.max_calls, self.max_resources, self.timeout = max_calls, max_resources, timeout

    def collect(self, principal: Principal, scope: Scope) -> Inventory:
        if principal.tenant_id != scope.tenant_id or "assessor" not in principal.roles:
            raise AccessDenied("Discovery scope authorization failed")
        started, calls = monotonic(), 0
        config = Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2})

        def call(client, operation, **kwargs):
            nonlocal calls
            if calls >= self.max_calls or monotonic() - started >= self.timeout:
                raise DiscoveryBudgetExceeded("Discovery budget exhausted")
            calls += 1
            return getattr(client, operation)(**kwargs)

        identity = call(
            self.session.client("sts", region_name=scope.regions[0], config=config),
            "get_caller_identity",
        )
        if identity["Account"] != scope.account_id:
            raise AccessDenied("AWS caller account differs from approved scope")
        resources, gaps = [], ["Only EC2 VPC and subnet configuration is implemented"]

        def pages(client, operation, result_key):
            token, seen = None, set()
            while True:
                data = call(client, operation, **({"NextToken": token} if token else {}))
                yield from data.get(result_key, [])
                token = data.get("NextToken")
                if not token:
                    break
                if token in seen:
                    raise DiscoveryBudgetExceeded("Pagination token repeated")
                seen.add(token)

        for region in scope.regions:
            ec2 = self.session.client("ec2", region_name=region, config=config)
            for operation, key, resource_type in (
                ("describe_vpcs", "Vpcs", "AWS::EC2::VPC"),
                ("describe_subnets", "Subnets", "AWS::EC2::Subnet"),
            ):
                try:
                    for item in pages(ec2, operation, key):
                        if len(resources) >= self.max_resources:
                            raise DiscoveryBudgetExceeded("Resource budget exhausted")
                        tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                        # Absence of ownership tags is not evidence of manual ownership.
                        owner = (
                            "cloudformation" if "aws:cloudformation:stack-id" in tags else "unknown"
                        )
                        properties = {
                            "tags": {k: v for k, v in tags.items() if not k.startswith("aws:")}
                        }
                        blockers = []
                        if resource_type == "AWS::EC2::VPC":
                            resource_id = item["VpcId"]
                            properties.update(
                                cidrBlock=item["CidrBlock"], instanceTenancy=item["InstanceTenancy"]
                            )
                            for attr, target in (
                                ("enableDnsSupport", "enableDnsSupport"),
                                ("enableDnsHostnames", "enableDnsHostnames"),
                                (
                                    "enableNetworkAddressUsageMetrics",
                                    "enableNetworkAddressUsageMetrics",
                                ),
                            ):
                                result = call(
                                    ec2, "describe_vpc_attribute", VpcId=resource_id, Attribute=attr
                                )
                                properties[target] = result[attr[0].upper() + attr[1:]]["Value"]
                            if item.get("IsDefault"):
                                blockers.append("DEFAULT_VPC_REQUIRES_SPECIAL_ADAPTER")
                            if len(item.get("CidrBlockAssociationSet", [])) != 1:
                                blockers.append("SECONDARY_OR_UNKNOWN_CIDR_ASSOCIATIONS")
                            if item.get("Ipv6CidrBlockAssociationSet"):
                                blockers.append("IPV6_REQUIRES_SPECIAL_ADAPTER")
                            dependencies = ()
                        else:
                            resource_id = item["SubnetId"]
                            properties.update(
                                vpcId=item["VpcId"],
                                cidrBlock=item["CidrBlock"],
                                availabilityZoneId=item["AvailabilityZoneId"],
                                mapPublicIpOnLaunch=item["MapPublicIpOnLaunch"],
                            )
                            options = item.get("PrivateDnsNameOptionsOnLaunch", {})
                            properties.update(
                                privateDnsHostnameTypeOnLaunch=options.get(
                                    "HostnameType", "ip-name"
                                ),
                                enableResourceNameDnsARecordOnLaunch=options.get(
                                    "EnableResourceNameDnsARecord", False
                                ),
                                enableResourceNameDnsAaaaRecordOnLaunch=options.get(
                                    "EnableResourceNameDnsAAAARecord", False
                                ),
                            )
                            if item.get("DefaultForAz"):
                                blockers.append("DEFAULT_SUBNET_REQUIRES_SPECIAL_ADAPTER")
                            if item.get("Ipv6CidrBlockAssociationSet") or item.get("Ipv6Native"):
                                blockers.append("IPV6_REQUIRES_SPECIAL_ADAPTER")
                            if item.get("OutpostArn") or item.get("MapCustomerOwnedIpOnLaunch"):
                                blockers.append("OUTPOST_OR_CUSTOMER_IP_UNSUPPORTED")
                            if item.get("EnableDns64"):
                                blockers.append("DNS64_UNSUPPORTED")
                            dependencies = (item["VpcId"],)
                        resources.append(
                            Resource(
                                resource_id=resource_id,
                                resource_type=resource_type,
                                account_id=scope.account_id,
                                region=region,
                                owner=owner,
                                dependencies=dependencies,
                                configuration=properties,
                                blockers=tuple(blockers),
                            )
                        )
                except (ClientError, BotoCoreError, DiscoveryBudgetExceeded, KeyError) as exc:
                    # Do not retain service exception text: it may contain customer identifiers.
                    gaps.append(f"{region}:{operation}:{type(exc).__name__}")
        return Inventory(
            scope=scope,
            provenance="aws_api",
            observed_at=datetime.now(UTC),
            coverage="partial",
            gaps=tuple(gaps),
            resources=tuple(resources),
        )
