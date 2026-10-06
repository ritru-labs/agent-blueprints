"""Runs the agent on one fixture in the sandbox and checks it against the fixture's manifest.

  python -m iac_agent.harness fixtures/out/F2-<run>.manifest.json [--model-id ID]
      [--second-run] [--cloudtrail-wait MIN] [--golden check|update]

Uses fixtures/out/sandbox.env (scripts/sandbox-setup.sh). Discovery runs as the
scanner role, Terraform as the importer role, each a short-lived session; your
own credentials only assume those roles, run drift.sh (F6), call Bedrock and
read CloudTrail. Approvals are given automatically: the scope is exactly the
manifest's adopt list. Checks (CLAUDE.md "Testing"): gates behave as the
manifest expects; state == adopt list; second run produces the same code;
CloudTrail shows no AWS writes by the agent; golden files for F1/F2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from langgraph.types import Command

from .core import hcl
from .core.gates import plan_gate
from .core.graph import NOT_CODE, Deps, build, start_state
from .core.models import GateOutcome, Manifest, ScopeItem
from .core.terraform import LOCK_FILE, sha256_file

ROOT = LOCK_FILE.parent
FIXTURES = ROOT / "fixtures"
GOLDEN = ROOT / "tests" / "golden"
CONFIG_LIMIT = 500


@dataclass
class Result:
    fixture: str
    run: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# --- Pure checks (unit-tested offline) -------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^export (\w+)=(\S*)$", path.read_text(), re.M))


def check_classification(manifest: Manifest, classifications: list[dict], candidates: list[dict]) -> list[str]:
    """Every adopt item must be a candidate; every exclude item must be discovered and not a candidate."""
    found = {(c["resource"]["terraform_type"], c["resource"]["import_id"]) for c in classifications}
    adoptable = {(c["terraform_type"], c["import_id"]) for c in candidates}
    problems = [f"expected adopt, not adoptable: {t} {i}" for t, i in sorted(manifest.keys("adopt") - adoptable)]
    for t, i in sorted(manifest.keys("exclude")):
        if (t, i) in adoptable:
            problems.append(f"expected exclude, but adoptable: {t} {i}")
        elif (t, i) not in found:
            problems.append(f"expected exclude, never discovered (missing from report): {t} {i}")
    return problems


def mutate_az(code: dict[str, str], address: str, before: str, after: str) -> dict[str, str]:
    """F7: the generated subnet moved to another AZ (availability_zone forces replacement)."""
    for name, text in code.items():
        block = hcl.get_block(text, address)
        if block and f'"{before}"' in block:
            return {**code, name: hcl.replace_block(text, address, block.replace(f'"{before}"', f'"{after}"'))}
    raise ValueError(f"{address} with {before} not found in generated code")


def normalize(text: str, manifest: Manifest) -> str:
    """Golden-file form: run-specific IDs and names become stable placeholders."""
    ids = sorted({r.import_id for r in manifest.resources} | {manifest.run, manifest.run.lower(), manifest.account},
                 key=len, reverse=True)  # fmt: skip
    for n, value in enumerate(ids):
        text = text.replace(value, f"<id{n}>")
    # Placeholder numbers depend on the set, not the values: renumber by first appearance.
    seen: dict[str, str] = {}
    return re.sub(r"<id\d+>", lambda m: seen.setdefault(m.group(0), f"<{len(seen) + 1}>"), text)


# --- Orchestration ---------------------------------------------------------------------


def _pending(graph, config) -> dict | None:
    found = [i.value for t in graph.get_state(config).tasks for i in t.interrupts]
    return found[0] if found else None


def drive(manifest: Manifest, deps: Deps, graph, result: Result, drift: Callable[[], None] | None = None) -> dict:
    """Runs one fixture through the pipeline, answering the human steps as the manifest expects."""
    config = {"configurable": {"thread_id": manifest.run}, "recursion_limit": CONFIG_LIMIT}
    graph.invoke(start_state(manifest.run, manifest.account, manifest.region), config)
    expect = manifest.expect.outcome
    mutation = getattr(manifest, "mutation", None)
    drifted = False
    while (ask := _pending(graph, config)) is not None:
        values = graph.get_state(config).values
        if ask["step"] == "scope_signoff":
            result.problems += check_classification(manifest, values["classifications"], ask["candidates"])
            graph.invoke(Command(resume=[list(k) for k in sorted(manifest.keys("adopt"))]), config)
        elif ask["step"] == "approve_plan" and mutation:
            result.problems += _check_forced_replacement(deps, values, mutation)
            graph.invoke(Command(resume=False), config)
        elif ask["step"] == "approve_plan" and drift and not drifted:
            drift()
            drifted = True
            graph.invoke(Command(resume=True), config)
        elif ask["step"] == "approve_plan":
            graph.invoke(Command(resume=True), config)
        else:  # llm_paused: never raise budgets automatically
            result.problems.append(f"run paused: {ask.get('reason')}")
            graph.invoke(Command(resume="stop"), config)

    values = graph.get_state(config).values
    status = values.get("status")
    scope = {s["address"]: (s["terraform_type"], s["import_id"]) for s in values.get("scope", [])}
    in_state = {scope.get(a, ("?", a)) for a in values.get("adopted", [])}
    if expect in ("pass", "restart"):
        wanted = "adopted" if manifest.keys("adopt") else "nothing_to_adopt"
        if status != wanted:
            result.problems.append(f"status {status}, expected {wanted}")
        if in_state != manifest.keys("adopt"):
            result.problems.append(f"state != adopt list: missing {sorted(manifest.keys('adopt') - in_state)}, "
                                   f"extra {sorted(in_state - manifest.keys('adopt'))}")  # fmt: skip
        if expect == "restart" and not values.get("restarts"):
            result.problems.append("drift was not detected: no restart")
    elif expect == "blocked":
        if status != "blocked":
            result.problems.append(f"status {status}, expected blocked")
        removed = (getattr(manifest, "scanner_policy", None) or {}).get("removed_service")
        if removed and f"coverage incomplete for aws_{removed}" not in json.dumps(values.get("gates", [])):
            result.problems.append(f"blocked, but not for the {removed} permission gap")
    elif expect == "hard_stop":
        if status != "rejected" or values.get("adopted"):
            result.problems.append(f"status {status} with {values.get('adopted')}, expected nothing imported")
    if expect != "blocked" and expect != "hard_stop":
        result.notes.append(f"skipped: {values.get('skipped') or 'none'}")
    return values


def _check_forced_replacement(deps: Deps, values: dict, mutation: dict) -> list[str]:
    address = next(s["address"] for s in values["scope"] if s["import_id"] == mutation["import_id"])
    code = {p.name: p.read_text() for p in deps.workdir.glob("*.tf") if p.name not in NOT_CODE}
    for name, text in mutate_az(code, address, mutation["from"], mutation["to"]).items():
        (deps.workdir / name).write_text(text)
    planfile = deps.terraform.plan(out="tfplan-mutated")
    active = [ScopeItem(**s) for s in values["scope"] if s["address"] not in values.get("skipped", {})]
    gate = plan_gate(deps.terraform.show_json(planfile), active, sha256_file(planfile))
    hits = [f for f in gate.findings if f.address == address and f.outcome is GateOutcome.HARD_STOP]
    return [] if hits else [f"plan gate did not hard-stop on {address}: {gate.outcome.value}"]


def code_of(workdir: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(workdir.glob("*.tf")) if p.name not in NOT_CODE}


# --- AWS wiring (sandbox only) -----------------------------------------------------------


def _assume(base, role_arn: str, external_id: str, session_name: str, policy: str | None = None) -> dict:
    kwargs = {"RoleArn": role_arn, "RoleSessionName": session_name, "ExternalId": external_id}
    if policy:
        kwargs["Policy"] = policy
    return base.client("sts").assume_role(**kwargs)["Credentials"]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs the sandbox
    import boto3

    from .adapters.aws.adapter import AwsAdapter
    from .cli import use_pinned_tools
    from .core.repair import TIMEOUTS, BedrockRepairer
    from .core.scanners import Scanners
    from .core.terraform import Terraform

    p = argparse.ArgumentParser(prog="python -m iac_agent.harness")
    p.add_argument("manifest", type=Path)
    p.add_argument("--env", type=Path, default=FIXTURES / "out" / "sandbox.env")
    p.add_argument("--model-id")
    p.add_argument("--second-run", action="store_true", help="re-run from scratch; code must be identical")
    p.add_argument("--cloudtrail-wait", type=int, default=0, help="minutes to wait, then check CloudTrail")
    p.add_argument("--golden", choices=["check", "update"])
    args = p.parse_args(argv)
    use_pinned_tools()

    manifest = Manifest.model_validate_json(args.manifest.read_text())
    env = load_env(args.env)
    base = boto3.Session(region_name=manifest.region)
    if base.client("sts").get_caller_identity()["Account"] != manifest.account:
        print("Refusing: credentials are not for the manifest's sandbox account", file=sys.stderr)
        return 2
    os.environ.pop("AWS_PROFILE", None)  # Terraform must use the importer session only
    started = datetime.now(UTC)
    result = Result(manifest.fixture, manifest.run)

    policy = None
    if extra := getattr(manifest, "scanner_policy", None):
        policy = json.dumps(json.loads((args.manifest.parent / extra["file"]).read_text()), separators=(",", ":"))
    sessions = {"scan": f"iac-agent-scan-{manifest.run}", "import": f"iac-agent-import-{manifest.run}"}

    def deps_for(workdir: Path, state_key: str) -> Deps:
        workdir.mkdir(parents=True, exist_ok=True)
        sc = _assume(base, env["SCANNER_ROLE_ARN"], env["EXTERNAL_ID"], sessions["scan"], policy)
        im = _assume(base, env["IMPORTER_ROLE_ARN"], env["EXTERNAL_ID"], sessions["import"])
        scanner = boto3.Session(aws_access_key_id=sc["AccessKeyId"], aws_secret_access_key=sc["SecretAccessKey"],
                                aws_session_token=sc["SessionToken"], region_name=manifest.region)  # fmt: skip
        tf_env = {"AWS_ACCESS_KEY_ID": im["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": im["SecretAccessKey"],
                  "AWS_SESSION_TOKEN": im["SessionToken"], "AWS_REGION": manifest.region}  # fmt: skip
        backend = (f'terraform {{\n  backend "s3" {{\n    bucket       = "{env["STATE_BUCKET"]}"\n'
                   f'    key          = "{state_key}"\n    region       = "{manifest.region}"\n'
                   f'    encrypt      = true\n    kms_key_id   = "{env["STATE_KMS_KEY_ARN"]}"\n'
                   "    use_lockfile = true\n  }\n}\n")  # fmt: skip
        repairer = (BedrockRepairer(base.client("bedrock-runtime", config=TIMEOUTS), args.model_id)
                    if args.model_id else (lambda block, problems: ""))  # fmt: skip
        return Deps(adapter=AwsAdapter(scanner, manifest.region), terraform=Terraform(workdir, env=tf_env),
                    scanners=Scanners(), repairer=repairer, workdir=workdir, backend_hcl=backend)  # fmt: skip

    def drift() -> None:
        subprocess.run([str(FIXTURES / "F6-drift-mid-run" / "drift.sh"), manifest.run], check=True)

    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    workdir = FIXTURES / "out" / "runs" / f"{manifest.fixture}-{manifest.run}"
    with sqlite3.connect(workdir.parent / f"{workdir.name}.sqlite", check_same_thread=False) as conn:
        deps = deps_for(workdir, f"fixtures/{manifest.fixture}/{manifest.run}.tfstate")
        drive(manifest, deps, build(deps, SqliteSaver(conn)), result,
              drift if getattr(manifest, "drift", None) else None)  # fmt: skip

    if args.second_run and manifest.expect.outcome == "pass" and manifest.keys("adopt"):
        second = workdir.with_name(workdir.name + "-second")
        with sqlite3.connect(second.parent / f"{second.name}.sqlite", check_same_thread=False) as conn:
            deps2 = deps_for(second, f"fixtures/{manifest.fixture}/{manifest.run}-second.tfstate")
            graph = build(deps2, SqliteSaver(conn))
            config = {"configurable": {"thread_id": manifest.run}, "recursion_limit": CONFIG_LIMIT}
            graph.invoke(start_state(manifest.run, manifest.account, manifest.region), config)
            graph.invoke(Command(resume=[list(k) for k in sorted(manifest.keys("adopt"))]), config)
            if code_of(second) != code_of(workdir):
                result.problems.append("second run produced different code (not idempotent)")
            graph.invoke(Command(resume=False), config)  # reject: never import twice

    if args.golden and manifest.fixture in ("F1", "F2"):
        golden = GOLDEN / manifest.fixture
        got = {n: normalize(t, manifest) for n, t in code_of(workdir).items()}
        if args.golden == "update":
            golden.mkdir(parents=True, exist_ok=True)
            for n, t in got.items():
                (golden / n).write_text(t)
            result.notes.append(f"golden files written to {golden}: review before committing")
        elif got != {p.name: p.read_text() for p in golden.glob("*.tf")}:
            result.problems.append(f"generated code differs from {golden}")

    if args.cloudtrail_wait:
        time.sleep(args.cloudtrail_wait * 60)  # CloudTrail delivers events within about 15 minutes
        ct = base.client("cloudtrail")
        for name in sessions.values():
            pages = ct.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": name}], StartTime=started
            )
            writes = [e for page in pages for e in page["Events"]
                      if json.loads(e.get("CloudTrailEvent", "{}")).get("readOnly") is False]  # fmt: skip
            if writes:
                result.problems.append(f"{name} made AWS write calls: {sorted({e['EventName'] for e in writes})}")
        result.notes.append("CloudTrail checked")

    out = {"fixture": result.fixture, "run": result.run, "ok": result.ok, "problems": result.problems,
           "notes": result.notes}  # fmt: skip
    (workdir / "HARNESS_RESULT.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
