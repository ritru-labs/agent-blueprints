"""End-to-end pipeline runs against a fake cloud and a fake terraform binary.

The real Terraform wrapper is used with a fake runner, so the forbidden-command
and plan-hash guards are exercised for real. The fake binary builds plan JSON
from imports.tf and the generated .tf files the way Terraform would.
"""

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from iac_agent.core import hcl
from iac_agent.core.graph import Deps, build, start_state
from iac_agent.core.models import Classification, Coverage, Discovery, Ownership, Resource, Tier
from iac_agent.core.terraform import Terraform, tool_versions

ACCOUNT, REGION = "111122223333", "eu-west-1"


class FakeCloud:
    inline_blocks = {"aws_security_group": {"ingress"}}
    identity_attrs = {"aws_vpc": set()}
    secret_attrs = {"aws_instance": {"user_data"}}

    def __init__(self, resources, account=ACCOUNT, incomplete=()):
        self.resources, self.account, self.incomplete = list(resources), account, set(incomplete)
        self.discoveries = 0

    def identity(self):
        return self.account, REGION

    def discover(self):
        self.discoveries += 1
        cov = [Coverage(terraform_type=t, complete=t not in self.incomplete, error="AccessDenied")
               for t in sorted({r.terraform_type for r in self.resources} | self.incomplete)]  # fmt: skip
        return Discovery(account=self.account, region=REGION, resources=self.resources, coverage=cov)

    def classify(self, discovery):
        return [
            Classification(resource=r, ownership=Ownership.DEFAULT, tier=Tier.EXCLUDED, reason="default")
            if r.tags.get("default")
            else Classification(resource=r, ownership=Ownership.OURS, tier=Tier.CERTIFIED, reason="hand-built")
            for r in discovery.resources
        ]

    def provider_files(self, versions, account):
        return {"versions.tf": f"# terraform {versions['terraform']} account {account}\n"}

    def file_for(self, terraform_type):
        return "network.tf" if terraform_type in ("aws_vpc", "aws_subnet") else "other.tf"


class FakeTerraformCLI:
    """Enough of the terraform CLI for the pipeline. `body` adds HCL to generated blocks;
    `actions(address, block)` decides each planned action (default: no-op import)."""

    def __init__(self, body=None, actions=None, validate=None):
        self.body, self.actions = body or {}, actions or (lambda a, b: ["no-op"])
        self.validate_out = validate or {"valid": True, "diagnostics": []}
        self.calls, self.state = [], []

    def __call__(self, cmd, cwd, **kwargs):
        args, wd = cmd[1:], Path(cwd)
        self.calls.append(args)
        out, code = "", 0
        if args[0] == "version":
            lock = tool_versions()
            out = json.dumps({"terraform_version": lock["terraform"], "provider_selections": {
                "registry.terraform.io/hashicorp/aws": lock["terraform_provider_aws"]}})  # fmt: skip
        elif args[0] == "validate":
            out = json.dumps(self.validate_out)
        elif args[0] == "plan" and "-detailed-exitcode" in args:
            code = 0
        elif args[0] == "plan":
            out_file = next(a.split("=", 1)[1] for a in args if a.startswith("-out="))
            gen = next((a.split("=", 1)[1] for a in args if a.startswith("-generate-config-out=")), None)
            imports = re.findall(r'to = (\S+)\n  id = "(.*)"', (wd / "imports.tf").read_text())
            if gen:
                (wd / gen).write_text("\n".join(
                    f'resource "{a.split(".")[0]}" "{a.split(".")[1]}" {{\n  name = "{i}"\n{self.body.get(a, "")}}}\n'
                    for a, i in imports))  # fmt: skip
            (wd / out_file).write_text(json.dumps(self._plan(wd, imports)))
        elif args[0] == "show":
            out = Path(wd / args[-1]).read_text()
        elif args[0] == "apply":
            plan = json.loads(Path(args[-1]).read_text())
            self.state = [rc["address"] for rc in plan["resource_changes"]]
        elif args[:2] == ["state", "list"]:
            out = "\n".join(self.state)
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    def _plan(self, wd, imports):
        generated = "\n".join(p.read_text() for p in sorted(wd.glob("*.tf")))
        changes, config = [], []
        for address, import_id in imports:
            block = hcl.get_block(generated, address)
            if block is None:
                continue
            rtype, name = address.split(".")
            exprs = {}
            for key, value in re.findall(r"^\s*(\w+)\s*=\s*(.+)$", block, re.M):
                exprs[key] = ({"constant_value": json.loads(value)} if value.startswith('"')
                              else {"references": [value]})  # fmt: skip
            config.append({"address": address, "mode": "managed", "type": rtype, "name": name, "expressions": exprs})
            changes.append({"address": address, "mode": "managed", "type": rtype, "name": name,
                            "change": {"actions": self.actions(address, block), "before": {}, "after": {},
                                       "importing": {"id": import_id}}})  # fmt: skip
        return {
            "format_version": "1.2",
            "resource_changes": changes,
            "configuration": {"root_module": {"resources": config}},
        }


class FakeScanners:
    def __init__(self, leaks=None):
        self.leaks = leaks or (lambda wd: [])

    def tflint(self, wd):
        return []

    def gitleaks(self, wd):
        return self.leaks(wd)

    def checkov(self, wd):
        return [{"resource": "aws_vpc.vpc_main", "check_id": "CKV2_AWS_11", "check_name": "VPC flow logs"}]


VPC = Resource(terraform_type="aws_vpc", import_id="vpc-0a1", name="main")
SUBNET = Resource(terraform_type="aws_subnet", import_id="subnet-0b2", name="app", tags={"Owner": "team-a"})
DEFAULT_SG = Resource(terraform_type="aws_security_group", import_id="sg-0d0", tags={"default": "1"})


def make(tmp_path, cloud=None, cli=None, repairer=None, scanners=None, checkpointer=None, budget=200_000):
    cloud = cloud or FakeCloud([VPC, SUBNET, DEFAULT_SG])
    cli = cli or FakeTerraformCLI()
    repairer = repairer or (lambda block, problems: pytest.fail("LLM must not be called"))
    deps = Deps(adapter=cloud, terraform=Terraform(tmp_path, runner=cli), scanners=scanners or FakeScanners(),
                repairer=repairer, workdir=tmp_path, llm_token_budget=budget)  # fmt: skip
    graph = build(deps, checkpointer or InMemorySaver())
    return graph, cloud, cli


CONFIG = {"configurable": {"thread_id": "r1"}, "recursion_limit": 200}


def pending(graph):
    return [i.value for t in graph.get_state(CONFIG).tasks for i in t.interrupts]


def run_to_approval(graph, scope="all"):
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "scope_signoff"
    graph.invoke(Command(resume=scope), CONFIG)


def applied(cli):
    return [c for c in cli.calls if c[0] == "apply"]


def test_happy_path_adopts_exactly_the_signed_off_scope(tmp_path):
    graph, cloud, cli = make(tmp_path)
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    (ask,) = pending(graph)
    assert {c["import_id"] for c in ask["candidates"]} == {"vpc-0a1", "subnet-0b2"}  # default SG excluded

    graph.invoke(Command(resume="all"), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "approve_plan" and sorted(ask["imports"]) == ["aws_subnet.subnet_app", "aws_vpc.vpc_main"]
    assert not applied(cli)  # nothing applied before the human approves

    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted"
    assert state["adopted"] == ["aws_subnet.subnet_app", "aws_vpc.vpc_main"]
    assert len(applied(cli)) == 1 and cloud.discoveries == 2  # scan + re-scan before import
    assert not (tmp_path / "generated.tf").exists()
    assert not (tmp_path / "imports.tf").exists()  # kept for audit, then removed
    assert "aws_vpc.vpc_main" in (tmp_path / "audit" / "imports-r1.tf").read_text()
    assert sorted(hcl.blocks((tmp_path / "network.tf").read_text())) == ["aws_subnet.subnet_app", "aws_vpc.vpc_main"]
    assert "terraform plan" in (tmp_path / "README.md").read_text()  # client README on success
    report = (tmp_path / "ADOPTION_REPORT.md").read_text()
    assert "- Adopted: 2" in report and "- Excluded: 1" in report and "**adopted**" in report
    assert "**not fixed**" in (tmp_path / "FINDINGS.md").read_text()
    assert list((tmp_path / "state-backups").iterdir())  # backup taken before import
    assert not any(c[0] in ("destroy", "import") or any(a.startswith("-target") for a in c) for c in cli.calls)


def test_partial_scope_signoff_imports_only_that(tmp_path):
    graph, _, cli = make(tmp_path)
    run_to_approval(graph, scope=[["aws_vpc", "vpc-0a1"], ["aws_security_group", "sg-0d0"]])
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["adopted"] == ["aws_vpc.vpc_main"]
    assert "not adoptable, ignored: aws_security_group sg-0d0" in json.dumps(state["gates"])


def test_wrong_account_stops_before_any_read(tmp_path):
    graph, cloud, cli = make(tmp_path, cloud=FakeCloud([VPC], account="999999999999"))
    state = graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    assert state["status"] == "blocked" and cloud.discoveries == 0 and cli.calls == []
    assert "Run status: **blocked**" in (tmp_path / "ADOPTION_REPORT.md").read_text()


def test_incomplete_coverage_blocks(tmp_path):
    graph, _, cli = make(tmp_path, cloud=FakeCloud([VPC], incomplete={"aws_s3_bucket"}))
    state = graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    assert state["status"] == "blocked" and not pending(graph) and cli.calls == []
    assert "coverage incomplete for aws_s3_bucket" in (tmp_path / "ADOPTION_REPORT.md").read_text()


def test_replace_is_a_hard_stop_with_no_apply(tmp_path):
    cli = FakeTerraformCLI(actions=lambda a, b: ["delete", "create"] if a.startswith("aws_subnet") else ["no-op"])
    graph, _, cli = make(tmp_path, cli=cli)
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    state = graph.invoke(Command(resume="all"), CONFIG)
    assert state["status"] == "hard_stop" and not pending(graph) and not applied(cli)


def test_hardcoded_ids_are_rewritten_in_code_not_by_the_llm(tmp_path):
    cli = FakeTerraformCLI(body={"aws_subnet.subnet_app": '  vpc_id = "vpc-0a1"\n'})
    graph, _, cli = make(tmp_path, cli=cli)
    run_to_approval(graph)
    assert "vpc_id = aws_vpc.vpc_main.id" in (tmp_path / "network.tf").read_text()
    assert graph.invoke(Command(resume=True), CONFIG)["status"] == "adopted"


def test_llm_repairs_an_update_and_the_gate_rechecks(tmp_path):
    cli = FakeTerraformCLI(
        body={"aws_subnet.subnet_app": "  drift = true\n"},
        actions=lambda a, b: ["update"] if "drift" in b else ["no-op"],
    )
    seen = []

    def repairer(block, problems):
        seen.append((block, problems))
        return "```hcl\n" + block.replace("  drift = true\n", "") + "```"

    graph, _, cli = make(tmp_path, cli=cli, repairer=repairer)
    run_to_approval(graph)
    assert len(seen) == 1 and seen[0][1] == ["import would update, not no-op"]
    assert graph.invoke(Command(resume=True), CONFIG)["status"] == "adopted"
    assert graph.get_state(CONFIG).values["attempts"] == {"aws_subnet.subnet_app": 1}


def test_repair_gives_up_after_three_attempts_and_skips(tmp_path):
    cli = FakeTerraformCLI(body={"aws_subnet.subnet_app": "  drift = true\n"},
                           actions=lambda a, b: ["update"] if "drift" in b else ["no-op"])  # fmt: skip
    calls = []
    graph, _, cli = make(tmp_path, cli=cli, repairer=lambda b, p: calls.append(1) or b)
    run_to_approval(graph)
    assert len(calls) == 3
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted" and state["adopted"] == ["aws_vpc.vpc_main"]
    assert state["skipped"]["aws_subnet.subnet_app"].startswith("repair failed 3 times")
    assert "aws_subnet.subnet_app" not in (tmp_path / "audit" / "imports-r1.tf").read_text()
    assert (
        "| aws_subnet.subnet_app | subnet-0b2 | repair failed 3 times" in (tmp_path / "ADOPTION_REPORT.md").read_text()
    )


INSTANCE = Resource(terraform_type="aws_instance", import_id="i-0c3", name="app")


def leak_at(marker):
    """A fake gitleaks that reports the line holding `marker`, wherever it is now."""

    def scan(wd):
        return [{"File": str(p), "StartLine": n, "RuleID": "generic-api-key"}
                for p in sorted(wd.glob("*.tf")) for n, line in enumerate(p.read_text().splitlines(), 1)
                if marker in line]  # fmt: skip

    return scan


@pytest.mark.parametrize(
    ("body", "scanner", "reason"),
    [
        ('  password = "hunter2"\n', leak_at("hunter2"), "secret value in generated code; never sent to the LLM"),
        (
            '  user_data = "#!/bin/sh"\n  drift = true\n',
            leak_at("never-matches"),
            "needs LLM repair but holds a secret-bearing attribute; never sent",
        ),
    ],
)
def test_secret_blocks_are_skipped_and_never_sent(tmp_path, body, scanner, reason):
    cli = FakeTerraformCLI(body={"aws_instance.instance_app": body},
                           actions=lambda a, b: ["update"] if "drift" in b else ["no-op"])  # fmt: skip
    graph, _, _ = make(tmp_path, cloud=FakeCloud([VPC, INSTANCE]), cli=cli, scanners=FakeScanners(scanner))
    run_to_approval(graph)  # the default repairer fails the test if it is ever called
    assert graph.get_state(CONFIG).values["skipped"] == {"aws_instance.instance_app": reason}
    assert "hunter2" not in json.dumps(graph.get_state(CONFIG).values["gates"])
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted" and state["adopted"] == ["aws_vpc.vpc_main"]


def test_drift_before_import_restarts_and_imports_fresh_code(tmp_path):
    graph, cloud, cli = make(tmp_path)
    run_to_approval(graph)
    cloud.resources[1] = SUBNET.model_copy(update={"tags": {"Owner": "team-b"}})  # F6: tag changed
    graph.invoke(Command(resume=True), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "approve_plan"  # scope approval kept; the new plan needs a new approval
    assert not applied(cli) and graph.get_state(CONFIG).values["restarts"] == 1
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted" and len(applied(cli)) == 1


def test_endless_drift_blocks_after_max_restarts(tmp_path):
    class Drifting(FakeCloud):
        def discover(self):
            self.resources[0] = VPC.model_copy(update={"tags": {"n": str(self.discoveries)}})
            return super().discover()

    graph, _, cli = make(tmp_path, cloud=Drifting([VPC]))
    run_to_approval(graph)
    for _ in range(4):
        if pending(graph):
            graph.invoke(Command(resume=True), CONFIG)
    assert graph.get_state(CONFIG).values["status"] == "blocked" and not applied(cli)


def test_rejected_plan_imports_nothing(tmp_path):
    graph, _, cli = make(tmp_path)
    run_to_approval(graph)
    state = graph.invoke(Command(resume=False), CONFIG)
    assert state["status"] == "rejected" and not applied(cli)


def test_run_resumes_from_sqlite_in_a_new_process(tmp_path):
    db = tmp_path / "run.sqlite"
    with sqlite3.connect(db, check_same_thread=False) as conn:
        graph, _, _ = make(tmp_path, checkpointer=SqliteSaver(conn))
        graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    with sqlite3.connect(db, check_same_thread=False) as conn:  # e.g. the next CLI command
        graph, _, cli = make(tmp_path, checkpointer=SqliteSaver(conn))
        assert pending(graph)[0]["step"] == "scope_signoff"
        graph.invoke(Command(resume="all"), CONFIG)
        assert graph.invoke(Command(resume=True), CONFIG)["status"] == "adopted"


def test_cli_runs_scan_plan_approve_and_refuses_out_of_order(tmp_path, monkeypatch, capsys):
    from langgraph.checkpoint.sqlite import SqliteSaver as Saver

    from iac_agent import cli

    clis = []

    def fake_graph(args, conn):
        graph, _, fake = make(args.workdir, checkpointer=Saver(conn))
        clis.append(fake)
        return graph

    monkeypatch.setattr(cli, "_graph", fake_graph)
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--region", REGION]
    assert cli.main(["scan", "--account", ACCOUNT, *base]) == 0
    assert json.loads((tmp_path / "scope_signoff.json").read_text())["candidates"]
    with pytest.raises(SystemExit):
        cli.main(["approve", *base])  # waiting for scope sign-off, not plan approval
    assert cli.main(["plan", *base]) == 0
    assert (tmp_path / "approve_plan.json").exists()
    assert cli.main(["approve", *base]) == 0
    assert "adopted" in capsys.readouterr().out
    assert sum(len(applied(c)) for c in clis) == 1


class CountingRepairer:
    """Never fixes anything; each call 'uses' 600 tokens. Optionally fails like Bedrock."""

    def __init__(self, error=None):
        self.calls, self.last_tokens, self.error = 0, 0, error

    def __call__(self, block, problems):
        if self.error:
            raise self.error
        self.calls += 1
        self.last_tokens = 600
        return block


def drifting_subnet_cli():
    return FakeTerraformCLI(body={"aws_subnet.subnet_app": "  drift = true\n"},
                            actions=lambda a, b: ["update"] if "drift" in b else ["no-op"])  # fmt: skip


def test_token_budget_pauses_the_run_and_a_bigger_budget_resumes_it(tmp_path):
    repairer = CountingRepairer()
    graph, _, cli = make(tmp_path, cli=drifting_subnet_cli(), repairer=repairer, budget=1000)
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    graph.invoke(Command(resume="all"), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "llm_paused" and ask["tokens_used"] == 1200 and "budget of 1000" in ask["reason"]
    assert repairer.calls == 2 and not applied(cli)

    graph.invoke(Command(resume={"budget": 5000}), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "approve_plan" and repairer.calls == 3  # third try, then skipped
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted" and state["llm_tokens"] == 1800
    assert "aws_subnet.subnet_app" in state["skipped"]


def test_llm_outage_pauses_then_resumes(tmp_path):
    from botocore.exceptions import ClientError

    down = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")
    repairer = CountingRepairer(error=down)
    graph, _, cli = make(tmp_path, cli=drifting_subnet_cli(), repairer=repairer)
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    graph.invoke(Command(resume="all"), CONFIG)
    (ask,) = pending(graph)
    assert ask["step"] == "llm_paused" and "ThrottlingException" in ask["reason"]
    assert graph.get_state(CONFIG).values["attempts"] == {}  # an outage costs no attempt

    repairer.error = None
    graph.invoke(Command(resume={"budget": 200_000}), CONFIG)
    assert pending(graph)[0]["step"] == "approve_plan"


def test_stop_after_an_llm_pause_imports_nothing(tmp_path):
    graph, _, cli = make(tmp_path, cli=drifting_subnet_cli(), repairer=CountingRepairer(), budget=0)
    graph.invoke(start_state("r1", ACCOUNT, REGION), CONFIG)
    graph.invoke(Command(resume="all"), CONFIG)
    state = graph.invoke(Command(resume="stop"), CONFIG)
    assert state["status"] == "failed" and not applied(cli)
    assert "budget of 0 used" in (tmp_path / "ADOPTION_REPORT.md").read_text()


def test_imports_stay_if_the_plan_is_not_clean_without_them(tmp_path):
    class DirtyAfterImportsRemoved(FakeTerraformCLI):
        def __call__(self, cmd, cwd, **kwargs):
            result = super().__call__(cmd, cwd, **kwargs)
            if "-detailed-exitcode" in cmd and not (Path(cwd) / "imports.tf").exists():
                result.returncode = 2
            return result

    graph, _, _ = make(tmp_path, cli=DirtyAfterImportsRemoved())
    run_to_approval(graph)
    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "failed" and (tmp_path / "imports.tf").exists()
    assert "plan without imports.tf = 2" in (tmp_path / "ADOPTION_REPORT.md").read_text()
