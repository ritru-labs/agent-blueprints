"""The whole pipeline against the real pinned terraform binary, no cloud needed.

hashicorp/random resources can be imported without any cloud account, so this
proves the real CLI behaviour the AWS runs depend on: import blocks plus
-generate-config-out with -out, plan JSON for a no-op import and for a forced
replacement, apply of the saved plan, state list and exit codes.

Opt-in (downloads a provider): scripts/install-tools.sh, then
IAC_AGENT_REAL_TOOLS=1 pytest tests/test_real_terraform_pipeline.py
"""

import json
import os

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from iac_agent.core import hcl
from iac_agent.core.gates import plan_gate
from iac_agent.core.graph import NOT_CODE, Deps, build, start_state
from iac_agent.core.models import (
    Classification,
    Coverage,
    Discovery,
    GateOutcome,
    Ownership,
    Resource,
    ScopeItem,
    Tier,
)
from iac_agent.core.scanners import Scanners
from iac_agent.core.terraform import LOCK_FILE, Terraform, sha256_file

TOOLS = LOCK_FILE.parent / ".tools" / "bin"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("IAC_AGENT_REAL_TOOLS") and TOOLS.is_dir()), reason="opt-in: real pinned tools"
)
RANDOM_PROVIDER = "3.9.1"
CONFIG = {"configurable": {"thread_id": "real"}, "recursion_limit": 200}
NUMBERS = [
    Resource(terraform_type="random_integer", import_id="15390,1,50000", name="priority"),
    Resource(terraform_type="random_integer", import_id="7,1,10", name="weight"),
]


class RandomCloud:
    """A 'cloud' whose resources are random_integer values: importable with no account."""

    inline_blocks: dict = {}
    identity_attrs: dict = {}
    secret_attrs: dict = {}

    def identity(self):
        return "local", "local"

    def discover(self):
        return Discovery(account="local", region="local", resources=NUMBERS,
                         coverage=[Coverage(terraform_type="random_integer", complete=True, count=2)])  # fmt: skip

    def classify(self, discovery):
        return [Classification(resource=r, ownership=Ownership.OURS, tier=Tier.CERTIFIED, reason="test")
                for r in discovery.resources]  # fmt: skip

    def provider_files(self, versions, account):
        return {"versions.tf": (
            f'terraform {{\n  required_version = "= {versions["terraform"]}"\n  required_providers {{\n'
            f'    random = {{\n      source  = "hashicorp/random"\n      version = "= {RANDOM_PROVIDER}"\n'
            "    }\n  }\n}\n"
        )}  # fmt: skip

    def file_for(self, terraform_type):
        return "random.tf"


@pytest.fixture
def graph_and_tf(tmp_path):
    os.environ["PATH"] = f"{TOOLS}{os.pathsep}{os.environ['PATH']}"
    tf = Terraform(tmp_path)
    deps = Deps(adapter=RandomCloud(), terraform=tf, scanners=Scanners(),
                repairer=lambda b, p: pytest.fail("no repair expected"), workdir=tmp_path)  # fmt: skip
    return build(deps, InMemorySaver()), tf, tmp_path


def test_real_terraform_adopts_with_zero_changes(graph_and_tf):
    graph, tf, wd = graph_and_tf
    graph.invoke(start_state("real", "local", "local"), CONFIG)
    graph.invoke(Command(resume="all"), CONFIG)
    (ask,) = [i.value for t in graph.get_state(CONFIG).tasks for i in t.interrupts]
    assert ask["step"] == "approve_plan", graph.get_state(CONFIG).values.get("gates")
    assert sorted(ask["imports"]) == ["random_integer.integer_priority", "random_integer.integer_weight"]

    state = graph.invoke(Command(resume=True), CONFIG)
    assert state["status"] == "adopted", json.dumps(state["gates"], indent=1)
    assert sorted(tf.state_list()) == ["random_integer.integer_priority", "random_integer.integer_weight"]
    assert tf.detailed_exitcode() == 0 and not (wd / "imports.tf").exists()
    code = (wd / "random.tf").read_text()
    assert "min" in code and "max" in code and "generate-config-out" in code
    assert (wd / "FINDINGS.md").exists() and (wd / "README.md").exists()


def test_real_plan_json_for_a_forced_replacement_is_a_hard_stop(graph_and_tf):
    graph, tf, wd = graph_and_tf
    graph.invoke(start_state("real", "local", "local"), CONFIG)
    graph.invoke(Command(resume="all"), CONFIG)
    values = graph.get_state(CONFIG).values
    scope = [ScopeItem(**s) for s in values["scope"]]

    code = (wd / "random.tf").read_text()  # F7-style mutation of the generated code
    block = hcl.get_block(code, "random_integer.integer_priority")
    (wd / "random.tf").write_text(hcl.replace_block(code, "random_integer.integer_priority",
                                                    block.replace("50000", "60000")))  # fmt: skip
    planfile = tf.plan(out="tfplan-mutated")
    gate = plan_gate(tf.show_json(planfile), scope, sha256_file(planfile))
    assert gate.outcome is GateOutcome.HARD_STOP
    (f,) = [f for f in gate.findings if f.outcome is GateOutcome.HARD_STOP]
    assert f.address == "random_integer.integer_priority" and f.message.startswith("replace")
    assert ["max"] in f.detail["replace_paths"]
    assert {p.name for p in wd.glob("*.tf")} - NOT_CODE == {"random.tf"}
    graph.invoke(Command(resume=False), CONFIG)
    assert tf.state_list() == []  # nothing was ever applied
