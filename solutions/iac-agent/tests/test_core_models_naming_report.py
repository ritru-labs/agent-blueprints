"""Models (incl. real fixture manifests), deterministic naming, and the two reports."""

import json
import os
import subprocess
from pathlib import Path

from iac_agent.core.models import (
    Classification,
    Coverage,
    Finding,
    GateOutcome,
    GateResult,
    Manifest,
    Ownership,
    Resource,
    ScopeItem,
    Tier,
)
from iac_agent.core.naming import addresses
from iac_agent.core.report import adoption_report, findings_report

ROOT = Path(__file__).resolve().parent.parent


def test_fingerprint_changes_with_tags_only_when_they_change():
    a = Resource(terraform_type="aws_vpc", import_id="vpc-1", tags={"Owner": "team-a"})
    assert (
        a.fingerprint() == Resource(terraform_type="aws_vpc", import_id="vpc-1", tags={"Owner": "team-a"}).fingerprint()
    )
    assert (
        a.fingerprint() != Resource(terraform_type="aws_vpc", import_id="vpc-1", tags={"Owner": "team-b"}).fingerprint()
    )


def test_gate_outcome_takes_the_most_severe_finding():
    r = GateResult(
        gate="plan",
        findings=[
            Finding(outcome=GateOutcome.REPAIR, message="a"),
            Finding(outcome=GateOutcome.HARD_STOP, message="b"),
        ],
    )
    assert r.outcome is GateOutcome.HARD_STOP and not r.passed
    assert GateResult(gate="plan").passed


def test_every_fixture_manifest_parses(tmp_path):
    """Builds each fixture with the stub aws and loads its manifest into the Manifest model."""
    env = {
        "PATH": f"{ROOT / 'tests' / 'stub_aws'}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AWS_STUB_STATE": str(tmp_path),
        "AWS_STUB_ACCOUNT": "111122223333",
        "SANDBOX_ACCOUNT_ID": "111122223333",
        "AWS_REGION": "eu-west-1",
        "FIXTURE_OUT_DIR": str(tmp_path / "out"),
        "FIXTURE_RETRY_SECONDS": "0",
    }
    for script in sorted((ROOT / "fixtures").glob("F*/create.sh")):
        subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    manifests = {
        m.fixture: m
        for m in (Manifest.model_validate_json(p.read_text()) for p in (tmp_path / "out").glob("*.manifest.json"))
    }
    assert sorted(manifests) == ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    assert manifests["F2"].expected_state() == manifests["F2"].keys("adopt")
    assert manifests["F7"].expected_state() == set() and manifests["F7"].keys("adopt")
    assert manifests["F6"].expected_state() == manifests["F6"].keys("adopt")


def test_addresses_are_readable_stable_and_unique():
    rs = [
        Resource(terraform_type="aws_vpc", import_id="vpc-0abc12345678", name="Main VPC (prod)"),
        Resource(terraform_type="aws_subnet", import_id="subnet-0aaa11111111", name="app"),
        Resource(terraform_type="aws_subnet", import_id="subnet-0bbb22222222", name="app"),
        Resource(terraform_type="aws_route_table", import_id="rtb-0ccc33333333"),
        Resource(terraform_type="aws_vpc", import_id="vpc-0ddd44444444", name="日本語"),
        Resource(terraform_type="aws_s3_bucket_policy", import_id="my.bucket", name="my.bucket"),
    ]
    got = addresses(rs)
    assert got == {
        ("aws_vpc", "vpc-0abc12345678"): "aws_vpc.vpc_main_vpc_prod",
        ("aws_subnet", "subnet-0aaa11111111"): "aws_subnet.subnet_app_11111111",
        ("aws_subnet", "subnet-0bbb22222222"): "aws_subnet.subnet_app_22222222",
        ("aws_route_table", "rtb-0ccc33333333"): "aws_route_table.route_table_33333333",
        ("aws_vpc", "vpc-0ddd44444444"): "aws_vpc.vpc_44444444",
        ("aws_s3_bucket_policy", "my.bucket"): "aws_s3_bucket_policy.s3_bucket_policy_my_bucket",
    }
    assert addresses(reversed(rs)) == got
    assert len(set(got.values())) == len(got)


def test_adoption_report_lists_every_resource_once():
    vpc = Resource(terraform_type="aws_vpc", import_id="vpc-1", name="a|b")
    sub = Resource(terraform_type="aws_subnet", import_id="subnet-1")
    sg = Resource(terraform_type="aws_security_group", import_id="sg-default")
    extra = Resource(terraform_type="aws_vpc", import_id="vpc-2")
    classes = [
        Classification(resource=vpc, ownership=Ownership.OURS, tier=Tier.CERTIFIED, reason="hand-built"),
        Classification(resource=sub, ownership=Ownership.OURS, tier=Tier.CERTIFIED, reason="hand-built"),
        Classification(resource=sg, ownership=Ownership.DEFAULT, tier=Tier.EXCLUDED, reason="default SG | x"),
        Classification(resource=extra, ownership=Ownership.OURS, tier=Tier.CERTIFIED, reason="hand-built"),
    ]
    scope = [
        ScopeItem(terraform_type="aws_vpc", import_id="vpc-1", address="aws_vpc.a"),
        ScopeItem(terraform_type="aws_subnet", import_id="subnet-1", address="aws_subnet.s"),
    ]
    md = adoption_report(
        account="111122223333",
        region="eu-west-1",
        run_id="r1",
        versions={"terraform": "1.16.5"},
        classifications=classes,
        scope=scope,
        adopted={"aws_vpc.a"},
        skipped={"aws_subnet.s": "repair failed 3x"},
        coverage=[Coverage(terraform_type="aws_vpc", complete=True, count=2)],
        gates=[GateResult(gate="plan")],
    )
    assert "- Adopted: 1" in md and "- Skipped: 1" in md and "- Excluded: 1" in md
    assert "| aws_subnet.s | subnet-1 | repair failed 3x |" in md
    assert "default SG \\| x" in md
    assert "| aws_vpc | vpc-2 |" in md  # adoptable but not signed off
    for rid in ["vpc-1", "subnet-1", "sg-default", "vpc-2"]:
        assert md.count(f" {rid} |") == 1, rid


def test_findings_report_is_report_only():
    md = findings_report([{"resource": "aws_s3_bucket.b", "check_id": "CKV_AWS_18", "check_name": "Access logging"}])
    assert "**not fixed**" in md and "CKV_AWS_18" in md
    assert "_None._" in findings_report([])
    json.dumps(md)
