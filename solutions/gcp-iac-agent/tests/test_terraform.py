import json
import os
import stat
import sys
from pathlib import Path

import pytest

from gcp_iac_agent.terraform import (
    GENERATED,
    Plan,
    TerraformError,
    UnsafeEdit,
    Workspace,
    check_generated,
    diagnostics,
    parse_plan_json,
    safe_defaults,
)

NETWORK = 'resource "google_compute_network" "prod_vpc" {\n  name = "prod-vpc"\n  mtu  = 1460\n}\n'
TYPES = {"google_compute_network"}


def imported(address, actions=("no-op",), before=None, after=None):
    change = {"actions": list(actions), "importing": {"id": "x"}, "before": before, "after": after}
    return {"address": address, "change": change}


def test_zero_change_requires_every_import_and_no_updates():
    clean = parse_plan_json({"resource_changes": [imported("a.b")]})
    assert Plan([], clean, expected_imports=1).zero_change
    assert not Plan([], clean, expected_imports=2).zero_change  # an import went missing

    drift = parse_plan_json(
        {
            "resource_changes": [
                imported("a.b", ["update"], {"mtu": 1460, "id": "x"}, {"mtu": 1500, "id": "x"})
            ]
        }
    )
    plan = Plan([], drift, expected_imports=1)
    assert not plan.zero_change
    assert plan.summary()["changes"] == [
        {"address": "a.b", "actions": ["update"], "diff": {"mtu": {"config": "1500", "cloud": "1460"}}}
    ]


def test_computed_attributes_are_not_reported_as_diff():
    change = imported("a.b", ["update"], {"mtu": 1460, "self_link": "s"}, {"mtu": 1500})
    change["change"]["after_unknown"] = {"self_link": True}
    assert parse_plan_json({"resource_changes": [change]})[0].diff == {
        "mtu": {"config": "1500", "cloud": "1460"}
    }


def test_errors_block_zero_change():
    assert not Plan(["boom"], [], expected_imports=0).zero_change


def test_diagnostics_reads_error_events_only():
    stream = "\n".join(
        [
            json.dumps({"@level": "info", "@message": "Plan: 1 to import"}),
            json.dumps(
                {
                    "@level": "error",
                    "diagnostic": {
                        "summary": "Conflicting configuration arguments",
                        "detail": "ipv4_range conflicts with auto_create_subnetworks",
                        "range": {"filename": "generated.tf"},
                    },
                }
            ),
            "not json",
        ]
    )
    assert diagnostics(stream) == [
        "generated.tf: Conflicting configuration arguments ipv4_range conflicts with auto_create_subnetworks"
    ]


@pytest.mark.parametrize(
    "text",
    [
        NETWORK.replace("}\n", "  lifecycle {\n    ignore_changes = all\n  }\n}\n"),
        NETWORK + 'data "external" "x" {\n  program = ["sh"]\n}\n',
        NETWORK + 'resource "google_compute_instance" "new" {\n}\n',
        NETWORK.replace("}\n", '  provisioner "local-exec" {\n    command = "id"\n  }\n}\n'),
        NETWORK + 'provider "google" {\n}\n',
    ],
)
def test_generated_config_rejects_hidden_drift_code_and_new_resources(text):
    with pytest.raises(UnsafeEdit):
        check_generated(text, TYPES)


def test_bucket_lifecycle_rule_is_still_allowed():
    check_generated(
        'resource "google_storage_bucket" "b" {\n  lifecycle_rule {\n    action {\n'
        '      type = "Delete"\n    }\n  }\n}\n',
        {"google_storage_bucket"},
    )


def test_edits_are_exact_and_all_or_nothing(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / GENERATED).write_text(NETWORK)
    ws.apply_edits(
        [{"resource": "google_compute_network.prod_vpc", "old": "mtu  = 1460", "new": "mtu  = 1500"}], TYPES
    )
    assert "1500" in ws.generated()

    with pytest.raises(UnsafeEdit):  # second edit is unsafe, so the first must not land either
        ws.apply_edits(
            [
                {
                    "resource": "google_compute_network.prod_vpc",
                    "old": 'name = "prod-vpc"',
                    "new": 'name = "other"',
                },
                {
                    "resource": "google_compute_network.prod_vpc",
                    "old": "mtu  = 1500",
                    "new": "mtu  = 1500\n  lifecycle {\n  }",
                },
            ],
            TYPES,
        )
    assert 'name = "prod-vpc"' in ws.generated()

    with pytest.raises(UnsafeEdit):
        ws.apply_edits(
            [{"resource": "google_compute_network.prod_vpc", "old": "does not exist", "new": ""}], TYPES
        )


# --- subprocess plumbing, against a stand-in terraform binary ------------------------------

FAKE_TERRAFORM = """#!{python}
import json, os, sys
args = sys.argv[1:]
plan = json.loads(open(os.environ["FAKE_PLAN"]).read()) if os.path.exists(os.environ["FAKE_PLAN"]) else {{}}
if args[0] == "init":
    sys.exit(0)
if args[:2] == ["state", "list"]:
    print(os.environ.get("FAKE_STATE", ""))
    sys.exit(0)
if args[0] == "plan" and "-detailed-exitcode" in args:
    sys.exit(int(os.environ.get("FAKE_VERIFY_EXIT", "0")))
if args[0] == "plan":
    out = next(a.split("=", 1)[1] for a in args if a.startswith("-out="))
    open(out, "w").write(json.dumps(plan))
elif args[0] == "show":
    print(open(args[-1]).read())
elif args[0] == "apply":
    open("applied", "w").write(args[-1])
"""


@pytest.fixture
def fake_terraform(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "terraform"
    script.write_text(FAKE_TERRAFORM.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    plan_file = tmp_path / "plan.json"
    monkeypatch.setenv("FAKE_PLAN", str(plan_file))
    work = tmp_path / "work"
    work.mkdir()
    return Workspace(work), plan_file


def test_import_applies_only_the_reviewed_import_only_plan(fake_terraform, monkeypatch):
    ws, plan_file = fake_terraform
    plan_file.write_text(json.dumps({"resource_changes": [imported("a.b")]}))
    plan = ws.plan(expected_imports=1)
    assert plan.zero_change and plan.sha256

    with pytest.raises(TerraformError, match="changed after review"):
        ws.import_reviewed_plan("0" * 64, expected_imports=1)
    assert not (ws.dir / "applied").exists()

    ws.import_reviewed_plan(plan.sha256, expected_imports=1)
    assert (ws.dir / "applied").read_text() == "tfplan"
    assert ws.verify_no_changes()
    monkeypatch.setenv("FAKE_VERIFY_EXIT", "2")
    assert not ws.verify_no_changes()


def test_import_refuses_a_plan_that_changes_resources(fake_terraform):
    ws, plan_file = fake_terraform
    plan_file.write_text(json.dumps({"resource_changes": [imported("a.b", ["update"], {"x": 1}, {"x": 2})]}))
    plan = ws.plan(expected_imports=1)
    with pytest.raises(TerraformError, match="not import-only"):
        ws.import_reviewed_plan(plan.sha256, expected_imports=1)
    assert not Path(ws.dir / "applied").exists()


FIREWALLS = "".join(
    f'resource "google_compute_firewall" "{n}" {{\n  source_tags = []\n  priority    = 1000\n}}\n\n'
    for n in ("a", "b", "c")
)


def test_same_text_in_several_resources_is_edited_per_resource(tmp_path):
    # The real failure on rithru-radf-ci: one fix, identical text in three firewall rules.
    ws = Workspace(tmp_path)
    (tmp_path / GENERATED).write_text(FIREWALLS)
    ws.apply_edits(
        [{"resource": "google_compute_firewall.b", "old": "  source_tags = []\n", "new": ""}],
        {"google_compute_firewall"},
    )
    text = ws.generated()
    assert text.count("source_tags = []") == 2
    assert 'resource "google_compute_firewall" "b" {\n  priority    = 1000\n}' in text

    with pytest.raises(UnsafeEdit, match="not found"):
        ws.apply_edits(
            [{"resource": "google_compute_firewall.zzz", "old": "priority", "new": "x"}],
            {"google_compute_firewall"},
        )


def test_scaffold_refuses_a_workspace_that_already_manages_resources(fake_terraform, monkeypatch):
    ws, _ = fake_terraform
    monkeypatch.setenv("FAKE_STATE", "google_compute_network.default")
    (ws.dir / GENERATED).write_text(NETWORK)
    with pytest.raises(TerraformError, match="already manages 1 resources"):
        ws.scaffold("p", [], ">= 6.0")
    assert (ws.dir / GENERATED).read_text() == NETWORK  # existing config untouched


def test_scaffold_writes_one_state_prefix_per_workspace(fake_terraform):
    ws, _ = fake_terraform
    ws.scaffold("p", [{"tf_type": "t", "tf_name": "n", "import_id": "i"}], ">= 6.0", state_bucket="b")
    backend = (ws.dir / "backend.tf").read_text()
    assert 'bucket = "b"' in backend and 'prefix = "gcp-iac-agent/work"' in backend


def test_project_services_never_disable_the_api_when_removed_from_terraform():
    text = (
        'resource "google_project_service" "iap" {\n  disable_on_destroy         = null\n'
        '  service                    = "iap.googleapis.com"\n}\n\n'
        'resource "google_compute_instance" "vm" {\n  disable_on_destroy = null\n}\n'
    )
    out = safe_defaults(text)
    assert 'resource "google_project_service" "iap" {\n  disable_on_destroy         = false\n' in out
    assert (
        'resource "google_compute_instance" "vm" {\n  disable_on_destroy = null\n' in out
    )  # other types untouched
