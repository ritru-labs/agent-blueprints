"""The sandbox harness, offline: its checks and its driving of every fixture outcome."""

from test_pipeline import ACCOUNT, DEFAULT_SG, REGION, SUBNET, VPC, FakeCloud, FakeTerraformCLI, applied, make

from iac_agent.core.graph import Deps
from iac_agent.core.models import Manifest
from iac_agent.core.terraform import Terraform
from iac_agent.harness import Result, check_classification, drive, load_env, mutate_az, normalize


def manifest(outcome="pass", step=None, **extra):
    return Manifest.model_validate({
        "fixture": "FX", "run": "20261006T000000Z", "account": ACCOUNT, "region": REGION,
        "expect": {"outcome": outcome, "step": step},
        "resources": [
            {"terraform_type": "aws_vpc", "import_id": "vpc-0a1", "expect": "adopt", "reason": "r"},
            {"terraform_type": "aws_subnet", "import_id": "subnet-0b2", "expect": "adopt", "reason": "r"},
            {"terraform_type": "aws_security_group", "import_id": "sg-0d0", "expect": "exclude", "reason": "r"},
        ],
        **extra,
    })  # fmt: skip


def run(tmp_path, m, cloud=None, cli=None, drift=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    graph, cloud, cli = make(tmp_path, cloud=cloud, cli=cli)
    result = Result(m.fixture, m.run)
    deps = Deps(adapter=cloud, terraform=Terraform(tmp_path, runner=cli), scanners=None, repairer=None,
                workdir=tmp_path)  # fmt: skip
    values = drive(m, deps, graph, result, drift)
    return result, values, cli


def test_pass_fixture_adopts_exactly_the_manifest(tmp_path):
    result, values, cli = run(tmp_path, manifest())
    assert result.ok, result.problems
    assert values["status"] == "adopted" and len(applied(cli)) == 1


def test_classification_mismatch_is_reported(tmp_path):
    m = manifest()
    m.resources[2].expect = "adopt"  # the default SG cannot be adopted
    m.resources.append(m.resources[0].model_copy(update={"import_id": "vpc-0zz", "expect": "exclude"}))
    result, _, _ = run(tmp_path, m)
    assert "expected adopt, not adoptable: aws_security_group sg-0d0" in result.problems
    assert "expected exclude, never discovered (missing from report): aws_vpc vpc-0zz" in result.problems


def test_restart_fixture_runs_the_drift_hook_once(tmp_path):
    cloud = FakeCloud([VPC, SUBNET, DEFAULT_SG])
    calls = []

    def drift():
        calls.append(1)
        cloud.resources[1] = SUBNET.model_copy(update={"tags": {"Owner": "team-b"}})

    result, values, cli = run(tmp_path, manifest("restart", "approve"), cloud=cloud, drift=drift)
    assert result.ok, result.problems
    assert calls == [1] and values["restarts"] == 1 and len(applied(cli)) == 1


def test_restart_fixture_fails_if_drift_is_missed(tmp_path):
    result, _, _ = run(tmp_path, manifest("restart", "approve"), drift=lambda: None)
    assert result.problems == ["drift was not detected: no restart"]


def test_hard_stop_fixture_mutates_the_az_and_the_plan_gate_stops(tmp_path):
    cli = FakeTerraformCLI(
        body={"aws_subnet.subnet_app": '  availability_zone = "eu-west-1a"\n'},
        actions=lambda a, b: ["delete", "create"] if '"eu-west-1b"' in b else ["no-op"],
    )
    m = manifest("hard_stop", "plan", mutation={"terraform_type": "aws_subnet", "import_id": "subnet-0b2",
                                                "attribute": "availability_zone", "from": "eu-west-1a",
                                                "to": "eu-west-1b"})  # fmt: skip
    result, values, cli = run(tmp_path, m, cli=cli)
    assert result.ok, result.problems
    assert values["status"] == "rejected" and not applied(cli)


def test_blocked_fixture(tmp_path):
    result, values, cli = run(tmp_path, manifest("blocked", "discover"),
                              cloud=FakeCloud([VPC], incomplete={"aws_s3_bucket"}))  # fmt: skip
    assert result.ok and values["status"] == "blocked" and not applied(cli)
    result, _, _ = run(tmp_path / "x", manifest("blocked", "discover"))
    assert result.problems == ["status adopted, expected blocked"]

    gap = {"scanner_policy": {"file": "p.json", "removed_service": "s3"}}
    result, _, _ = run(tmp_path / "y", manifest("blocked", "discover", **gap),
                       cloud=FakeCloud([VPC], incomplete={"aws_s3_bucket"}))  # fmt: skip
    assert result.ok, result.problems
    result, _, _ = run(tmp_path / "z", manifest("blocked", "discover", **gap),
                       cloud=FakeCloud([VPC], incomplete={"aws_iam_role"}))  # fmt: skip
    assert result.problems == ["blocked, but not for the s3 permission gap"]


def test_pure_helpers(tmp_path):
    env = tmp_path / "sandbox.env"
    env.write_text("# comment\nexport SCANNER_ROLE_ARN=arn:aws:iam::1:role/s\nexport EXTERNAL_ID=abc\n")
    assert load_env(env) == {"SCANNER_ROLE_ARN": "arn:aws:iam::1:role/s", "EXTERNAL_ID": "abc"}

    code = {"network.tf": 'resource "aws_subnet" "s" {\n  availability_zone = "eu-west-1a"\n}\n'}
    assert '"eu-west-1b"' in mutate_az(code, "aws_subnet.s", "eu-west-1a", "eu-west-1b")["network.tf"]

    m = manifest()
    a = 'resource "aws_vpc" "v" {\n  id = "vpc-0a1"\n  sub = "subnet-0b2"\n  owner = "111122223333"\n}'
    b = a.replace("vpc-0a1", "vpc-0ff").replace("subnet-0b2", "subnet-0ee")
    m2 = manifest()
    m2.resources[0].import_id, m2.resources[1].import_id = "vpc-0ff", "subnet-0ee"
    assert normalize(a, m) == normalize(b, m2)  # same shape, different run: same golden text
    assert "vpc-0a1" not in normalize(a, m) and "111122223333" not in normalize(a, m)


def test_classification_check_directly():
    m = manifest()
    classes = [{"resource": {"terraform_type": t, "import_id": i}} for t, i in m.keys("adopt") | m.keys("exclude")]
    cands = [{"terraform_type": t, "import_id": i} for t, i in m.keys("adopt")]
    assert check_classification(m, classes, cands) == []
    assert check_classification(m, classes, [*cands, {"terraform_type": "aws_security_group", "import_id": "sg-0d0"}])
