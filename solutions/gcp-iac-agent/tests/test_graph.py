from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from gcp_iac_agent.discover import Discovery, Resource
from gcp_iac_agent.graph import build_graph
from gcp_iac_agent.repair import Edit, RepairPlan
from gcp_iac_agent.terraform import UnsafeEdit

NETWORK = Resource(
    "compute.googleapis.com/Network",
    "google_compute_network",
    "prod_vpc",
    "projects/p/global/networks/prod-vpc",
)
CONFIG = {"configurable": {"thread_id": "p"}}


class FakeWorkspace:
    """Plan is clean once the config says mtu = 1460 (the live value)."""

    def __init__(self, reject_edits=False):
        self.config = "mtu = 1500"
        self.reject_edits, self.imported, self.plans = reject_edits, None, 0

    def scaffold(self, project, resources, provider_version, state_bucket=None):
        self.scaffolded = [r["import_id"] for r in resources]

    def generate_config(self):
        return []

    def plan(self, expected_imports):
        from gcp_iac_agent.terraform import Change, Plan

        self.plans += 1
        sha = f"{self.plans:064d}"
        if self.config == "mtu = 1460":
            return Plan(
                [], [Change("google_compute_network.prod_vpc", ["no-op"], True)], expected_imports, sha
            )
        diff = {"mtu": {"config": "1500", "cloud": "1460"}}
        return Plan(
            [], [Change("google_compute_network.prod_vpc", ["update"], True, diff)], expected_imports, sha
        )

    def generated(self):
        return self.config

    def apply_edits(self, edits, allowed_types):
        assert allowed_types == {"google_compute_network"}
        if self.reject_edits:
            raise UnsafeEdit("generated.tf may not contain lifecycle blocks")
        for e in edits:
            self.config = self.config.replace(e["old"], e["new"])

    def import_reviewed_plan(self, sha, expected_imports):
        self.imported = sha

    def verify_no_changes(self):
        return True


class FakeRepairer:
    def __init__(self, edits=True):
        self.calls, self.edits = [], edits

    def __call__(self, config, plan_summary, feedback):
        self.calls.append((plan_summary["changes"], list(feedback)))
        edits = (
            [Edit(resource="google_compute_network.prod_vpc", old="mtu = 1500", new="mtu = 1460")]
            if self.edits
            else []
        )
        return RepairPlan(edits=edits, notes="matched mtu to the live value")


def build(ws, repairer, **kw):
    return build_graph(
        workspace=ws,
        discover=lambda project, types: Discovery([NETWORK], ["x: not supported"]),
        repairer=repairer,
        checkpointer=InMemorySaver(),
        **kw,
    )


def test_repairs_to_zero_change_then_imports_after_approval():
    ws, repairer = FakeWorkspace(), FakeRepairer()
    graph = build(ws, repairer)
    graph.invoke({"project": "p"}, CONFIG)

    state = graph.get_state(CONFIG)
    assert state.next == ("review",)
    assert ws.imported is None  # nothing touches state before a human approves
    assert repairer.calls[0][0][0]["diff"] == {"mtu": {"config": "1500", "cloud": "1460"}}
    review = state.tasks[0].interrupts[0].value
    assert review["resources"] == ["google_compute_network.prod_vpc <- projects/p/global/networks/prod-vpc"]

    graph.invoke(Command(resume={"approve": True, "plan_sha256": review["plan_sha256"]}), CONFIG)
    final = graph.get_state(CONFIG).values
    assert final["status"] == "DONE"
    assert ws.imported == review["plan_sha256"]


def test_approval_for_a_different_plan_is_rejected():
    ws = FakeWorkspace()
    graph = build(ws, FakeRepairer())
    graph.invoke({"project": "p"}, CONFIG)
    graph.invoke(Command(resume={"approve": True, "plan_sha256": "f" * 64}), CONFIG)
    assert graph.get_state(CONFIG).values["status"] == "REJECTED"
    assert ws.imported is None


def test_stops_for_a_human_when_repair_budget_is_spent():
    ws, repairer = FakeWorkspace(reject_edits=True), FakeRepairer()
    graph = build(ws, repairer, max_attempts=2)
    graph.invoke({"project": "p"}, CONFIG)

    values = graph.get_state(CONFIG).values
    assert values["status"] == "NEEDS_HUMAN"
    assert len(repairer.calls) == 2
    assert "edits rejected" in repairer.calls[1][1][0]  # the model sees why its last attempt failed
    assert ws.imported is None


def test_model_giving_up_ends_the_loop_immediately():
    repairer = FakeRepairer(edits=False)
    graph = build(FakeWorkspace(), repairer, max_attempts=5)
    graph.invoke({"project": "p"}, CONFIG)
    assert graph.get_state(CONFIG).values["status"] == "NEEDS_HUMAN"
    assert len(repairer.calls) == 1


def test_nothing_discovered_ends_cleanly():
    graph = build_graph(
        workspace=FakeWorkspace(),
        discover=lambda project, types: Discovery([], []),
        repairer=FakeRepairer(),
        checkpointer=InMemorySaver(),
    )
    graph.invoke({"project": "p"}, CONFIG)
    assert graph.get_state(CONFIG).values["status"] == "NOTHING_TO_IMPORT"
