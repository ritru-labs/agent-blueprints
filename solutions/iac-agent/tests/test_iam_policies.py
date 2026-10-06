"""Offline checks that the agent's IAM policies cannot change AWS.

These run without AWS credentials. scripts/verify-iam-cannot-write.sh adds a
live check with the IAM policy simulator once a sandbox account exists.
"""

import fnmatch
import json
from pathlib import Path

import pytest

IAM_DIR = Path(__file__).resolve().parent.parent / "iam"
SCANNER = json.loads((IAM_DIR / "scanner-policy.json").read_text())
IMPORTER = json.loads((IAM_DIR / "importer-policy.template.json").read_text())

READ_VERBS = ("Describe", "Get", "List", "Head")
STATE_WRITES = {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
STATE_KMS = {"kms:Decrypt", "kms:GenerateDataKey"}

# Actions that must never be allowed for either identity.
FORBIDDEN = [
    "ec2:RunInstances",
    "ec2:TerminateInstances",
    "ec2:CreateVpc",
    "ec2:DeleteVpc",
    "ec2:ModifyVpcAttribute",
    "ec2:AuthorizeSecurityGroupIngress",
    "ec2:RevokeSecurityGroupIngress",
    "ec2:CreateTags",
    "ec2:DeleteTags",
    "ec2:GetPasswordData",
    "ec2:GetConsoleOutput",
    "iam:CreateRole",
    "iam:PutRolePolicy",
    "iam:AttachRolePolicy",
    "iam:PassRole",
    "iam:CreateAccessKey",
    "s3:CreateBucket",
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
    "secretsmanager:GetSecretValue",
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "cloudformation:CreateStack",
    "cloudformation:DeleteStack",
    "sts:AssumeRole",
]


def statements(policy, effect):
    return [s for s in policy["Statement"] if s["Effect"] == effect]


def as_list(value):
    return value if isinstance(value, list) else [value]


def allowed_actions(policy):
    return [a for s in statements(policy, "Allow") for a in as_list(s["Action"])]


def matches(action, patterns):
    return any(fnmatch.fnmatchcase(action.lower(), p.lower()) for p in patterns)


def effective(policy, action, resource="*"):
    """Tiny evaluator: explicit deny wins, then allow, else implicit deny."""
    for s in statements(policy, "Deny"):
        if "NotAction" in s and matches(action, as_list(s["NotAction"])):
            continue
        if "Action" in s and not matches(action, as_list(s["Action"])):
            continue
        if "NotResource" in s and resource != "*" and matches(resource, as_list(s["NotResource"])):
            continue
        return "explicitDeny"
    for s in statements(policy, "Allow"):
        if matches(action, as_list(s["Action"])):
            res = as_list(s["Resource"])
            if resource == "*" or matches(resource, res) or res == ["*"]:
                return "allowed"
    return "implicitDeny"


def test_no_wildcard_service_or_action():
    for policy in (SCANNER, IMPORTER):
        for action in allowed_actions(policy):
            assert action != "*"
            assert not action.endswith(":*"), action


def test_scanner_allows_only_read_verbs():
    for action in allowed_actions(SCANNER):
        verb = action.split(":", 1)[1]
        assert verb.startswith(READ_VERBS), action


def test_importer_writes_only_state():
    for action in allowed_actions(IMPORTER):
        verb = action.split(":", 1)[1]
        if verb.startswith(READ_VERBS) and action not in STATE_WRITES:
            continue
        assert action in STATE_WRITES | STATE_KMS, action
    for s in statements(IMPORTER, "Allow"):
        if set(as_list(s["Action"])) & (STATE_WRITES | STATE_KMS):
            assert as_list(s["Resource"]) != ["*"], s["Sid"]


@pytest.mark.parametrize("action", FORBIDDEN)
@pytest.mark.parametrize("policy", [SCANNER, IMPORTER], ids=["scanner", "importer"])
def test_forbidden_actions_are_explicitly_denied(policy, action):
    assert effective(policy, action) == "explicitDeny"


def test_scanner_cannot_read_objects_or_secrets():
    for action in ["s3:GetObject", "s3:GetObjectVersion", "kms:Decrypt"]:
        assert effective(SCANNER, action) == "explicitDeny", action


def test_importer_object_access_limited_to_state_bucket():
    state_obj = "arn:aws:s3:::REPLACE_STATE_BUCKET/env/terraform.tfstate"
    other_obj = "arn:aws:s3:::customer-data/report.csv"
    assert effective(IMPORTER, "s3:PutObject", state_obj) == "allowed"
    assert effective(IMPORTER, "s3:GetObject", other_obj) == "explicitDeny"
    assert effective(IMPORTER, "s3:PutObject", other_obj) == "explicitDeny"


def test_reads_needed_for_adoption_are_allowed():
    for policy in (SCANNER, IMPORTER):
        for action in [
            "ec2:DescribeVpcs",
            "ec2:DescribeInstanceAttribute",
            "iam:GetRole",
            "iam:ListAttachedRolePolicies",
            "s3:GetBucketPolicy",
            "s3:ListBucket",
            "cloudformation:DescribeStackResources",
        ]:
            if policy is IMPORTER and action.startswith("cloudformation:"):
                continue
            assert effective(policy, action) == "allowed", action
