"""Gates on recorded plan JSON (tests/data/plans): pass, update, replace, delete, partial import."""

import copy
import json
from pathlib import Path

import pytest

from iac_agent.core.gates import (
    config_gate,
    coverage_gate,
    fingerprint_gate,
    plan_gate,
    static_gate,
    verify_gate,
)
from iac_agent.core.models import Coverage, GateOutcome, ScopeItem

PLANS = Path(__file__).parent / "data" / "plans"
VPC, SUB, BKT = "vpc-0a1b2c3d4e5f60718", "subnet-0f1e2d3c4b5a69788", "iacfx-f1-demo-111122223333"
SCOPE = [
    ScopeItem(terraform_type="aws_vpc", import_id=VPC, address="aws_vpc.vpc_f1_vpc"),
    ScopeItem(terraform_type="aws_subnet", import_id=SUB, address="aws_subnet.subnet_f1_public_a"),
    ScopeItem(
        terraform_type="aws_s3_bucket", import_id=BKT, address="aws_s3_bucket.s3_bucket_iacfx_f1_demo_111122223333"
    ),
]
INLINE = {"aws_security_group": {"ingress", "egress"}, "aws_route_table": {"route"}}
IDENTITY = {"aws_s3_bucket": {"bucket"}, "aws_iam_instance_profile": {"name"}}


def load(name):
    return json.loads((PLANS / f"{name}.json").read_text())


def test_pass_plan_passes_and_records_hash():
    result = plan_gate(load("pass"), SCOPE, plan_sha256="abc")
    assert result.passed, result.findings
    assert result.detail["plan_sha256"] == "abc"


def test_update_goes_to_repair_with_attribute_names_only():
    result = plan_gate(load("update"), SCOPE)
    assert result.outcome is GateOutcome.REPAIR
    (f,) = result.findings
    assert f.address == "aws_subnet.subnet_f1_public_a"
    assert f.detail["changed"] == ["tags_all"]
    assert "team-a" not in json.dumps(f.model_dump())  # values never copied out


def test_replace_is_a_hard_stop():
    result = plan_gate(load("replace"), SCOPE)
    assert result.outcome is GateOutcome.HARD_STOP
    (f,) = result.findings
    assert f.message.startswith("replace")
    assert f.detail["replace_paths"] == [["availability_zone"]]


def test_delete_is_a_hard_stop_even_out_of_scope():
    result = plan_gate(load("delete"), SCOPE)
    assert result.outcome is GateOutcome.HARD_STOP
    assert [f.address for f in result.findings] == ["aws_vpc.old"]
    assert result.findings[0].message.startswith("delete")


def test_partial_import_reports_missing_resource():
    result = plan_gate(load("partial_import"), SCOPE)
    assert result.outcome is GateOutcome.REPAIR
    assert [f.message for f in result.findings] == ["in-scope resource missing from plan"]


def test_hard_stop_outranks_repair():
    plan = load("replace")
    plan["resource_changes"][0]["change"]["actions"] = ["update"]
    assert plan_gate(plan, SCOPE).outcome is GateOutcome.HARD_STOP


def test_out_of_scope_create_and_wrong_import_id():
    plan = load("pass")
    extra = copy.deepcopy(plan["resource_changes"][0])
    extra["address"], extra["change"]["actions"] = "aws_vpc.extra", ["create"]
    del extra["change"]["importing"]
    plan["resource_changes"].append(extra)
    plan["resource_changes"][1]["change"]["importing"]["id"] = "subnet-0000"
    by_addr = {f.address: f for f in plan_gate(plan, SCOPE).findings}
    assert by_addr["aws_vpc.extra"].outcome is GateOutcome.REPAIR
    assert by_addr["aws_subnet.subnet_f1_public_a"].outcome is GateOutcome.FAIL


def test_no_op_without_import_and_errored_plan_fail():
    plan = load("pass")
    del plan["resource_changes"][0]["change"]["importing"]
    plan["errored"] = True
    result = plan_gate(plan, SCOPE)
    assert result.outcome is GateOutcome.FAIL
    assert {f.message for f in result.findings} == {"plan errored", "in-scope resource is not imported"}


def test_data_sources_are_ignored():
    plan = load("pass")
    plan["resource_changes"].append(
        {"address": "data.aws_caller_identity.me", "mode": "data", "change": {"actions": ["read"]}}
    )
    assert plan_gate(plan, SCOPE).passed


def test_config_gate_passes_references_and_own_ids():
    assert config_gate(load("pass"), SCOPE, INLINE, IDENTITY).passed


def test_config_gate_flags_hardcoded_ids_and_inline_blocks():
    plan = load("pass")
    resources = plan["configuration"]["root_module"]["resources"]
    resources[1]["expressions"]["vpc_id"] = {"constant_value": VPC}
    resources.append(
        {
            "address": "aws_security_group.sg_web",
            "mode": "managed",
            "type": "aws_security_group",
            "name": "sg_web",
            "expressions": {"ingress": [{"from_port": {"constant_value": 443}}], "vpc_id": {"constant_value": VPC}},
        }
    )
    resources.append(
        {
            "address": "aws_s3_bucket_policy.p",
            "mode": "managed",
            "type": "aws_s3_bucket_policy",
            "name": "p",
            "expressions": {"bucket": {"constant_value": BKT}},
        }
    )
    result = config_gate(
        plan,
        [*SCOPE, ScopeItem(terraform_type="aws_s3_bucket_policy", import_id=BKT, address="aws_s3_bucket_policy.p")],
        INLINE,
        IDENTITY,
    )
    messages = sorted((f.address, f.message) for f in result.findings)
    assert messages == [
        (
            "aws_s3_bucket_policy.p",
            "bucket: hardcoded ID; reference aws_s3_bucket.s3_bucket_iacfx_f1_demo_111122223333 instead",
        ),
        ("aws_security_group.sg_web", "inline 'ingress' is not allowed; use separate resources"),
        ("aws_security_group.sg_web", "vpc_id: hardcoded ID; reference aws_vpc.vpc_f1_vpc instead"),
        ("aws_subnet.subnet_f1_public_a", "vpc_id: hardcoded ID; reference aws_vpc.vpc_f1_vpc instead"),
    ]


def test_config_gate_allows_instance_profile_named_like_its_role():
    plan = {
        "configuration": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_iam_instance_profile.app",
                        "mode": "managed",
                        "type": "aws_iam_instance_profile",
                        "expressions": {"name": {"constant_value": "app"}, "role": {"constant_value": "app"}},
                    },
                ]
            }
        }
    }
    scope = [
        ScopeItem(terraform_type="aws_iam_role", import_id="app", address="aws_iam_role.app"),
        ScopeItem(terraform_type="aws_iam_instance_profile", import_id="app", address="aws_iam_instance_profile.app"),
    ]
    (f,) = config_gate(plan, scope, INLINE, IDENTITY).findings
    assert f.detail["attribute"] == "role"


def test_config_gate_fails_provisioners():
    plan = load("pass")
    plan["configuration"]["root_module"]["resources"][0]["provisioners"] = [{"type": "local-exec"}]
    assert config_gate(plan, SCOPE, INLINE, IDENTITY).outcome is GateOutcome.FAIL


@pytest.mark.parametrize(
    "body",
    [
        "lifecycle {\n    ignore_changes = [tags]\n  }",
        'provisioner "local-exec" {}',
    ],
)
def test_static_gate_forbidden_hcl_inside_a_block_is_repaired(body):
    text = f'resource "aws_vpc" "a" {{\n  {body}\n}}\n'
    (f,) = static_gate({"main.tf": text}, [], {"valid": True}).findings
    assert (f.outcome, f.address) == (GateOutcome.REPAIR, "aws_vpc.a")


@pytest.mark.parametrize(
    "text", ['data "external" "x" {}', 'resource "null_resource" "x" {}', 'resource "terraform_data" "x" {}']
)
def test_static_gate_forbidden_hcl_outside_adopted_blocks_fails(text):
    result = static_gate({"main.tf": text}, [], {"valid": True})
    assert result.outcome in (GateOutcome.FAIL, GateOutcome.REPAIR) and not result.passed


def test_static_gate_tools_locate_the_block():
    text = (
        'resource "aws_vpc" "a" {\n  cidr_block = "10.0.0.0/16"\n}\n\n'
        'resource "aws_instance" "b" {\n  user_data = "x"\n}\n'
    )
    rng = lambda line: {"filename": "generated.tf", "start": {"line": line}}  # noqa: E731
    validate = {
        "valid": False,
        "diagnostics": [{"severity": "error", "summary": "Unsupported argument", "range": rng(2)}],
    }
    secrets = [
        {"File": "/tmp/w/generated.tf", "StartLine": 6, "RuleID": "aws-access-token", "Secret": "AKIAXXXXXXXXXXXXXXXX"}
    ]
    tflint = [
        {"rule": {"severity": "error"}, "message": "bad type", "range": rng(2)},
        {"rule": {"severity": "notice"}, "message": "x"},
    ]
    result = static_gate({"generated.tf": text}, ["generated.tf"], validate, tflint, secrets)
    got = sorted((f.outcome.value, f.address, f.detail.get("kind")) for f in result.findings)
    assert got == [
        ("fail", None, None),  # fmt is run by the pipeline; still unformatted means a tool problem
        ("repair", "aws_instance.b", "secret"),
        ("repair", "aws_vpc.a", None),
        ("repair", "aws_vpc.a", None),
    ]
    assert "AKIA" not in json.dumps([f.model_dump() for f in result.findings])
    assert static_gate({"main.tf": ""}, [], {"valid": True}, [], []).passed


def test_coverage_gate_blocks_on_incomplete_types():
    result = coverage_gate([Coverage(terraform_type="aws_s3_bucket", complete=False, error="AccessDenied")])
    assert result.outcome is GateOutcome.BLOCKED
    assert coverage_gate([]).passed


def test_fingerprint_gate():
    assert fingerprint_gate({"a": "1"}, {"a": "1"}).passed
    result = fingerprint_gate({"a": "1", "b": "2"}, {"a": "9", "c": "3"})
    assert result.outcome is GateOutcome.BLOCKED
    assert result.detail["changed"] == ["a", "b", "c"]


def test_verify_gate():
    addrs = [s.address for s in SCOPE]
    assert verify_gate(0, 0, [*addrs, "data.aws_region.current"], SCOPE).passed
    result = verify_gate(2, 0, [addrs[0], addrs[0], "aws_vpc.stray"], SCOPE)
    messages = sorted(f.message for f in result.findings)
    assert messages == [
        "in scope but not in state",
        "in scope but not in state",
        "in state but not in scope",
        "in state more than once",
        "plan -detailed-exitcode = 2",
    ]
