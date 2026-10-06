"""Terraform wrapper: refused commands, version pinning and plan-bound apply (fake runner, no binary)."""

import json
import subprocess

import pytest

from iac_agent.core.gates import plan_gate
from iac_agent.core.models import Finding, GateOutcome, GateResult
from iac_agent.core.terraform import ForbiddenCommand, Terraform, TerraformError, sha256_file, tool_versions


class FakeRunner:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd[1:])
        code, out = self.outputs.get(cmd[1], (0, ""))
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="boom" if code not in (0, 2, 3) else "")


@pytest.mark.parametrize(
    "args",
    [
        ["destroy"],
        ["import", "aws_vpc.a", "vpc-1"],
        ["taint", "aws_vpc.a"],
        ["state", "rm", "aws_vpc.a"],
        ["state", "mv", "a", "b"],
        ["plan", "-target=aws_vpc.a"],
        ["plan", "-replace=aws_vpc.a"],
        ["plan", "-destroy"],
        ["apply", "-lock=false", "tfplan"],
        ["plan", "-refresh=false"],
    ],
)
def test_forbidden_commands_never_reach_the_binary(tmp_path, args):
    runner = FakeRunner()
    with pytest.raises(ForbiddenCommand):
        Terraform(tmp_path, runner=runner)._run(*args)
    assert runner.calls == []


def test_version_check_matches_lock(tmp_path):
    lock = tool_versions()
    good = {
        "terraform_version": lock["terraform"],
        "provider_selections": {"registry.terraform.io/hashicorp/aws": lock["terraform_provider_aws"]},
    }
    Terraform(tmp_path, runner=FakeRunner({"version": (0, json.dumps(good))})).check_versions()
    bad = {**good, "terraform_version": "1.15.8"}
    with pytest.raises(TerraformError, match="pinned"):
        Terraform(tmp_path, runner=FakeRunner({"version": (0, json.dumps(bad))})).check_versions()


def test_apply_only_the_gated_plan(tmp_path):
    planfile = tmp_path / "tfplan"
    planfile.write_bytes(b"plan-v1")
    runner = FakeRunner()
    tf = Terraform(tmp_path, runner=runner)
    passed = plan_gate({"resource_changes": []}, [], plan_sha256=sha256_file(planfile))

    failed = GateResult(gate="plan", findings=[Finding(outcome=GateOutcome.REPAIR, message="x")], detail=passed.detail)
    with pytest.raises(ForbiddenCommand):
        tf.apply(planfile, failed)
    with pytest.raises(ForbiddenCommand):
        tf.apply(planfile, GateResult(gate="static", detail=passed.detail))
    planfile.write_bytes(b"plan-v2")  # swapped after the gate ran
    with pytest.raises(ForbiddenCommand, match="differs"):
        tf.apply(planfile, passed)
    assert runner.calls == []

    planfile.write_bytes(b"plan-v1")
    tf.apply(planfile, passed)
    assert runner.calls == [["apply", "-no-color", "-input=false", str(planfile)]]


def test_exit_codes(tmp_path):
    tf = Terraform(tmp_path, runner=FakeRunner({"fmt": (3, "main.tf\n"), "plan": (2, "")}))
    assert tf.fmt_unformatted() == ["main.tf"]
    assert tf.detailed_exitcode() == 2
    with pytest.raises(TerraformError):
        Terraform(tmp_path, runner=FakeRunner({"plan": (1, "")})).detailed_exitcode()


def test_automation_env_and_cwd(tmp_path):
    seen = {}

    def runner(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="a\nb\n", stderr="")

    assert Terraform(tmp_path, runner=runner).state_list() == ["a", "b"]
    assert seen["cwd"] == tmp_path and seen["env"]["TF_INPUT"] == "0" and seen["check"] is False


def test_state_list_before_any_import_is_empty(tmp_path):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No state file was found!\n")

    assert Terraform(tmp_path, runner=runner).state_list() == []

    def broken(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error: AccessDenied on state bucket")

    with pytest.raises(TerraformError, match="AccessDenied"):
        Terraform(tmp_path, runner=broken).state_list()
