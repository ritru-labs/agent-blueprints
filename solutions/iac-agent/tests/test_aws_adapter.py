"""AWS adapter on recorded-shape API responses (tests/data/aws/account.json).

botocore's Stubber validates every request and response against the real
service models; no network is used. The scenario mixes F2-style hand-built
resources with F4-style CloudFormation, ASG, default-VPC and service-linked ones.
"""

import copy
import json
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from iac_agent.adapters.aws import import_ids as ids
from iac_agent.adapters.aws.best_effort import list_best_effort
from iac_agent.adapters.aws.discovery import discover
from iac_agent.adapters.aws.ownership import classify, state_keys
from iac_agent.core.models import Ownership, Tier

DATA = json.loads((Path(__file__).parent / "data" / "aws" / "account.json").read_text())
SCANNER = json.loads((Path(__file__).resolve().parent.parent / "iam" / "scanner-policy.json").read_text())


def stubbed(scenario, fail=None):
    """Client factory over Stubbers. fail: {service: (operation, error code)} replaces that call."""
    clients, stubbers = {}, []
    scenario = copy.deepcopy(scenario)  # botocore decodes some fields in place (IAM policy documents)
    for service, calls in scenario.items():
        if service.startswith("_"):
            continue
        client = boto3.client(service, region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="x")
        stub = Stubber(client)
        for op, response in calls:
            if fail and fail.get(service, (None,))[0] == op:
                stub.add_client_error(op, service_error_code=fail[service][1], http_status_code=403)
                continue  # the group stops here; later groups still get their recorded responses
            if "_error" in response:
                stub.add_client_error(op, service_error_code=response["_error"], http_status_code=404)
            else:
                stub.add_response(op, response)
        stub.activate()
        clients[service], stubbers = client, [*stubbers, stub]
    return (lambda service: clients[service]), stubbers


@pytest.fixture
def discovery():
    factory, stubbers = stubbed(DATA)
    result = discover(factory, "111122223333", "eu-west-1")
    for s in stubbers:
        s.assert_no_pending_responses()  # every recorded call was made, in order
    return result


def by_key(classifications):
    return {c.resource.key: c for c in classifications}


def test_discovery_is_complete_and_paginates(discovery):
    assert not discovery.incomplete
    vpcs = {r.import_id for r in discovery.resources if r.terraform_type == "aws_vpc"}
    assert vpcs == {"vpc-0aaa", "vpc-0def", "vpc-0cf0"}  # second page read
    for r in discovery.resources:
        assert ids.is_valid(r.terraform_type, r.import_id), r.key


def test_only_created_ipv4_routes_and_non_main_associations(discovery):
    keys = {r.key for r in discovery.resources}
    assert ("aws_route", "rtb-0b0_0.0.0.0/0") in keys
    assert not any(k[0] == "aws_route" and k[1].startswith("rtb-0a0") for k in keys)  # local route only
    assert not any("pl-" in k[1] for k in keys)  # prefix-list route: not IPv4 CIDR
    assocs = {k[1] for k in keys if k[0] == "aws_route_table_association"}
    assert assocs == {"subnet-0a1/rtb-0b0"}


def test_s3_reads_only_configured_sub_resources(discovery):
    s3 = {r.key for r in discovery.resources if r.terraform_type.startswith("aws_s3")}
    assert {t for t, b in s3 if b == "app-bucket"} == set(ids.PATTERNS) & {
        t for t in ids.CERTIFIED if t.startswith("aws_s3_bucket")
    }
    assert {t for t, b in s3 if b == "bare-bucket"} == {
        "aws_s3_bucket",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_ownership_controls",
    }


ADOPT = {
    ("aws_vpc", "vpc-0aaa"),
    ("aws_subnet", "subnet-0a1"),
    ("aws_subnet", "subnet-0b1"),  # hand-built inside the default VPC
    ("aws_internet_gateway", "igw-0aaa"),
    ("aws_nat_gateway", "nat-0aaa"),
    ("aws_eip", "eipalloc-0aaa"),
    ("aws_route_table", "rtb-0b0"),
    ("aws_route", "rtb-0b0_0.0.0.0/0"),
    ("aws_route_table_association", "subnet-0a1/rtb-0b0"),
    ("aws_security_group", "sg-0e0"),
    ("aws_vpc_security_group_ingress_rule", "sgr-0e1"),
    ("aws_vpc_security_group_egress_rule", "sgr-0e2"),
    ("aws_instance", "i-0a0"),
    ("aws_ebs_volume", "vol-0a2"),
    ("aws_volume_attachment", "/dev/sdf:vol-0a2:i-0a0"),
    ("aws_key_pair", "ops-key"),
    ("aws_iam_role", "app-role"),
    ("aws_iam_role_policy", "app-role:inline-logs"),
    ("aws_iam_role_policy_attachment", "app-role/arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"),
    ("aws_iam_role_policy_attachment", "app-role/arn:aws:iam::111122223333:policy/app-read"),
    ("aws_iam_policy", "arn:aws:iam::111122223333:policy/app-read"),
    ("aws_iam_instance_profile", "app-role"),
    *(
        (t, "app-bucket")
        for t in [
            "aws_s3_bucket",
            "aws_s3_bucket_versioning",
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_policy",
            "aws_s3_bucket_lifecycle_configuration",
            "aws_s3_bucket_ownership_controls",
        ]
    ),
    *(
        (t, "bare-bucket")
        for t in [
            "aws_s3_bucket",
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_ownership_controls",
        ]
    ),
}

EXCLUDE = {
    ("aws_vpc", "vpc-0def"): Ownership.DEFAULT,
    ("aws_subnet", "subnet-0d1"): Ownership.DEFAULT,
    ("aws_internet_gateway", "igw-0def"): Ownership.DEFAULT,
    ("aws_route_table", "rtb-0a0"): Ownership.DEFAULT,
    ("aws_route_table", "rtb-0d0"): Ownership.DEFAULT,
    ("aws_route", "rtb-0d0_0.0.0.0/0"): Ownership.DEFAULT,
    ("aws_network_acl", "acl-0aaa"): Ownership.DEFAULT,
    ("aws_security_group", "sg-0d0"): Ownership.DEFAULT,
    ("aws_vpc_security_group_ingress_rule", "sgr-0d1"): Ownership.DEFAULT,
    ("aws_vpc", "vpc-0cf0"): Ownership.OTHER_TOOL,
    ("aws_subnet", "subnet-0c1"): Ownership.OTHER_TOOL,
    ("aws_security_group", "sg-0cf"): Ownership.OTHER_TOOL,
    ("aws_vpc_security_group_egress_rule", "sgr-0c1"): Ownership.OTHER_TOOL,
    ("aws_launch_template", "lt-0cf"): Ownership.OTHER_TOOL,
    ("aws_instance", "i-0f0"): Ownership.OTHER_TOOL,
    ("aws_ebs_volume", "vol-0a1"): Ownership.PART_OF_PARENT,
    ("aws_ebs_volume", "vol-0f1"): Ownership.PART_OF_PARENT,
    ("aws_network_interface", "eni-0a1"): Ownership.PART_OF_PARENT,
    ("aws_network_interface", "eni-0f1"): Ownership.PART_OF_PARENT,
    ("aws_network_interface", "eni-0a0"): Ownership.CLOUD_MANAGED,
    ("aws_iam_role", "AWSServiceRoleForAutoScaling"): Ownership.CLOUD_MANAGED,
    ("aws_iam_policy", "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"): Ownership.CLOUD_MANAGED,
}


def test_classification_matches_expectations(discovery):
    got = by_key(classify(discovery))
    assert {k for k, c in got.items() if c.adoptable} == ADOPT
    assert {k: c.ownership for k, c in got.items() if not c.adoptable} == EXCLUDE
    assert set(got) == ADOPT | EXCLUDE.keys()  # every discovered resource is classified once
    assert all(c.tier is Tier.CERTIFIED for c in got.values() if c.adoptable)
    assert all(c.reason for c in got.values())


def test_reasons_name_the_owner(discovery):
    got = by_key(classify(discovery))
    assert got[("aws_security_group", "sg-0cf")].reason == "owned by CloudFormation stack iacfx-F4"
    assert "sg-0cf" in got[("aws_vpc_security_group_egress_rule", "sgr-0c1")].reason
    assert "Auto Scaling" in got[("aws_instance", "i-0f0")].reason


def test_resources_in_client_state_are_excluded_with_their_rules(discovery):
    tfstate = {
        "resources": [
            {"mode": "managed", "type": "aws_security_group", "instances": [{"attributes": {"id": "sg-0e0"}}]},
            {
                "mode": "managed",
                "type": "aws_route",
                "instances": [
                    {"attributes": {"id": "r-x", "route_table_id": "rtb-0b0", "destination_cidr_block": "0.0.0.0/0"}}
                ],
            },
            {"mode": "data", "type": "aws_vpc", "instances": [{"attributes": {"id": "vpc-0aaa"}}]},
        ]
    }
    keys = state_keys(tfstate)
    assert keys == {("aws_security_group", "sg-0e0"), ("aws_route", "rtb-0b0_0.0.0.0/0")}
    got = by_key(classify(discovery, keys))
    assert got[("aws_security_group", "sg-0e0")].ownership is Ownership.IN_STATE
    assert got[("aws_vpc_security_group_ingress_rule", "sgr-0e1")].ownership is Ownership.IN_STATE
    assert got[("aws_route", "rtb-0b0_0.0.0.0/0")].ownership is Ownership.IN_STATE
    assert got[("aws_route_table", "rtb-0b0")].adoptable  # parent stays ours


@pytest.mark.parametrize(
    ("service", "op", "group"),
    [
        ("s3", "list_buckets", "aws_s3_bucket"),
        ("s3", "get_bucket_policy", "aws_s3_bucket_policy"),
        ("ec2", "describe_security_group_rules", "aws_security_group"),
        ("iam", "list_attached_role_policies", "aws_iam_role"),
    ],
)
def test_access_denied_marks_the_whole_group_incomplete(service, op, group):
    factory, _ = stubbed(DATA, fail={service: (op, "AccessDenied")})
    result = discover(factory, "111122223333", "eu-west-1")
    incomplete = {c.terraform_type for c in result.incomplete}
    assert group in incomplete
    assert all(c.error == f"AccessDenied: {''.join(w.title() for w in op.split('_'))}" for c in result.incomplete)
    assert not any(r.terraform_type in incomplete for r in result.resources)  # never silently partial


def test_missing_ownership_signals_block():
    factory, _ = stubbed(DATA, fail={"cloudformation": ("list_stacks", "AccessDenied")})
    result = discover(factory, "111122223333", "eu-west-1")
    assert {c.terraform_type for c in result.incomplete} == {"signal:cloudformation", "signal:autoscaling"}


# Operation -> IAM action where the names differ (S3 authorization reference).
S3_ACTIONS = {
    "ListBuckets": "s3:ListAllMyBuckets",
    "GetBucketEncryption": "s3:GetEncryptionConfiguration",
    "GetBucketLifecycleConfiguration": "s3:GetLifecycleConfiguration",
    "GetPublicAccessBlock": "s3:GetBucketPublicAccessBlock",
}


def test_every_discovery_call_is_allowed_by_the_scanner_policy():
    """Discovery must work with exactly the read-only scanner role, nothing more."""
    from test_iam_policies import effective

    factory, stubbers = stubbed(DATA)
    discover(factory, "111122223333", "eu-west-1")
    for service, calls in DATA.items():
        if service.startswith("_"):
            continue
        client = factory(service)
        prefix = client.meta.service_model.signing_name
        for op, _ in calls:
            name = client.meta.method_to_api_mapping[op]
            action = S3_ACTIONS.get(name, f"{prefix}:{name}")
            assert effective(SCANNER, action) == "allowed", action


def test_best_effort_is_opt_in_and_maps_identifiers():
    client = boto3.client("cloudcontrol", region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    stub.add_response(
        "list_resources",
        {"TypeName": "AWS::Logs::LogGroup", "ResourceDescriptions": [{"Identifier": "/app/web", "Properties": "{}"}]},
    )
    stub.add_client_error("list_resources", service_error_code="AccessDeniedException", http_status_code=403)
    stub.activate()
    resources, coverage = list_best_effort(lambda s: client, ["AWS::Logs::LogGroup", "AWS::SNS::Topic", "AWS::X::Y"])
    assert [(r.terraform_type, r.import_id) for r in resources] == [("aws_cloudwatch_log_group", "/app/web")]
    assert "Properties" not in json.dumps([r.model_dump() for r in resources])
    assert [(c.terraform_type, c.complete) for c in coverage] == [
        ("aws_cloudwatch_log_group", True),
        ("aws_sns_topic", False),
        ("AWS::X::Y", False),
    ]
    assert list_best_effort(lambda s: client, []) == ([], [])


def test_best_effort_resources_get_the_best_effort_tier():
    from iac_agent.core.models import Discovery, Resource

    r = Resource(terraform_type="aws_sqs_queue", import_id="https://sqs/q", attributes={"best_effort": True})
    (c,) = classify(Discovery(account="1", region="r", resources=[r]))
    assert c.tier is Tier.BEST_EFFORT and c.adoptable


def test_import_id_builders_and_state_parsing():
    assert ids.route("rtb-1", "0.0.0.0/0") == "rtb-1_0.0.0.0/0"
    assert ids.volume_attachment("/dev/sdf", "vol-1", "i-1") == "/dev/sdf:vol-1:i-1"
    assert ids.from_state("aws_iam_role_policy_attachment", {"role": "r", "policy_arn": "arn:x"}) == "r/arn:x"
    assert ids.from_state("aws_route_table_association", {"gateway_id": "igw-1", "route_table_id": "rtb-1"}) == (
        "igw-1/rtb-1"
    )
    assert ids.from_state("aws_route", {"id": "r-abc"}) == "r-abc"  # incomplete attrs: fall back to id
    assert not ids.is_valid("aws_vpc", "subnet-1") and not ids.is_valid("aws_unknown", "x")


def test_every_certified_type_has_a_service_file():
    from iac_agent.adapters.aws.adapter import AwsAdapter

    files = {t: AwsAdapter.file_for(AwsAdapter, t) for t in ids.CERTIFIED}
    assert set(files.values()) == {"network.tf", "security_groups.tf", "s3.tf", "iam.tf", "ec2.tf"}
    assert files["aws_vpc_security_group_egress_rule"] == "security_groups.tf"
    assert files["aws_s3_bucket_policy"] == "s3.tf" and files["aws_iam_instance_profile"] == "iam.tf"
