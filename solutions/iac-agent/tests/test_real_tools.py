"""The real pinned binaries against sample agent output: no AWS, but real CLIs.

Opt-in (downloads the AWS provider): run scripts/install-tools.sh, then
IAC_AGENT_REAL_TOOLS=1 pytest tests/test_real_tools.py
"""

import os
import shutil
from pathlib import Path

import pytest

from iac_agent.adapters.aws.adapter import AwsAdapter
from iac_agent.core import hcl
from iac_agent.core.gates import static_gate
from iac_agent.core.models import ScopeItem
from iac_agent.core.scanners import Scanners
from iac_agent.core.terraform import LOCK_FILE, Terraform, tool_versions

TOOLS = LOCK_FILE.parent / ".tools" / "bin"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("IAC_AGENT_REAL_TOOLS") and TOOLS.is_dir()), reason="opt-in: real pinned tools"
)
SAMPLE = Path(__file__).parent / "data" / "hcl" / "sample.tf"
FAKE_TOKEN = "ghp_" + "Zq7Xw3Rt9Lk2Mn5Pb8Vc1Hj4Gf6Ds0Ea3Yu7"  # GitHub-PAT shape, not a real token


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    os.environ["PATH"] = f"{TOOLS}{os.pathsep}{os.environ['PATH']}"
    wd = tmp_path_factory.mktemp("real")
    adapter = AwsAdapter.__new__(AwsAdapter)  # provider files only; no AWS session needed
    adapter.region = "eu-west-1"
    for name, text in adapter.provider_files(tool_versions(), "111122223333").items():
        (wd / name).write_text(text)
    shutil.copy(SAMPLE, wd / "generated.tf")
    scope = [ScopeItem(terraform_type="aws_vpc", import_id="vpc-0a1", address="aws_vpc.vpc_main")]
    (wd / "imports.tf").write_text(hcl.import_blocks(scope))
    tf = Terraform(wd)
    tf.init()
    return wd, tf


def test_versions_providers_and_imports_are_valid_and_pinned(workdir):
    wd, tf = workdir
    tf.check_versions()  # real `terraform version -json` after init: 1.16.5 + aws 6.67.0
    tf.fmt_write()
    assert tf.fmt_unformatted() == []
    result = tf.validate()
    assert result["valid"], result


def test_scanners_parse_real_output_and_locate_blocks(workdir):
    wd, tf = workdir
    scanners = Scanners()
    assert scanners.tflint(wd) == []
    assert scanners.gitleaks(wd) == []
    failed = scanners.checkov(wd)
    assert failed and all({"check_id", "resource"} <= f.keys() for f in failed)

    text = (wd / "generated.tf").read_text()
    leaky = text.replace('description = "Web tier"', f'description = "token {FAKE_TOKEN}"')
    (wd / "generated.tf").write_text(leaky)
    try:
        leaks = scanners.gitleaks(wd)
        assert leaks, "gitleaks found nothing"
        result = static_gate({"generated.tf": leaky}, [], {"valid": True}, [], leaks)
        (finding,) = result.findings
        assert finding.address == "aws_security_group.security_group_web"
        assert finding.detail["kind"] == "secret" and FAKE_TOKEN not in finding.message
    finally:
        (wd / "generated.tf").write_text(text)
