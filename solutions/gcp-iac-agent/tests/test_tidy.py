import json
import subprocess

import pytest

from gcp_iac_agent.terraform import GENERATED, TerraformError
from gcp_iac_agent.tidy import reference_maps, rewrite_block, split_blocks, tidy

LINK = "https://www.googleapis.com/compute/v1/projects/p/regions/r/subnetworks/app"
STATE = {
    "values": {
        "root_module": {
            "resources": [
                {
                    "address": "google_compute_subnetwork.app",
                    "type": "google_compute_subnetwork",
                    "values": {
                        "self_link": LINK,
                        "id": "projects/p/regions/r/subnetworks/app",
                        "name": "app",
                    },
                },
                {
                    "address": "google_compute_router.r1",
                    "type": "google_compute_router",
                    "values": {"id": "projects/p/regions/r/routers/r1", "name": "r1"},
                },
                {
                    "address": "google_compute_network.a",
                    "type": "google_compute_network",
                    "values": {"name": "dup"},
                },
                {
                    "address": "google_compute_network.b",
                    "type": "google_compute_network",
                    "values": {"name": "dup"},
                },
            ]
        }
    }
}
GENERATED_TF = f'''# __generated__ by Terraform
resource "google_compute_router_nat" "nat" {{
  project = "p"
  router  = "r1"
  subnetwork {{
    name = "{LINK}"
  }}
}}

resource "google_compute_subnetwork" "app" {{
  self_link_copy = "{LINK}"
  network        = "dup"
}}

resource "google_project_service" "iap" {{
  service = "iap.googleapis.com"
}}
'''


def test_literals_become_references_but_never_self_or_ambiguous():
    literal, names = reference_maps(STATE)
    blocks = dict(split_blocks(GENERATED_TF))
    assert list(blocks) == [
        "google_compute_router_nat.nat",
        "google_compute_subnetwork.app",
        "google_project_service.iap",
    ]

    nat = rewrite_block(
        "google_compute_router_nat.nat", blocks["google_compute_router_nat.nat"], "p", literal, names
    )
    assert "project = var.project" in nat
    assert "router  = google_compute_router.r1.name" in nat
    assert "name = google_compute_subnetwork.app.self_link" in nat

    subnet = rewrite_block(
        "google_compute_subnetwork.app", blocks["google_compute_subnetwork.app"], "p", literal, names
    )
    assert f'self_link_copy = "{LINK}"' in subnet  # a resource never references itself
    assert 'network        = "dup"' in subnet  # two networks share that name: left as a literal


class FakeWorkspace:
    def __init__(self, directory, clean_after):
        self.dir, self.clean_after, self.checks = directory, clean_after, 0

    def verify_no_changes(self):
        self.checks += 1
        return True if self.checks == 1 else self.clean_after

    def terraform(self, *args):
        stdout = json.dumps(STATE) if args[0] == "show" else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")


def workspace_files(tmp_path):
    (tmp_path / GENERATED).write_text(GENERATED_TF)
    (tmp_path / "imports.tf").write_text("import {}\n")
    (tmp_path / "providers.tf").write_text('provider "google" {\n  project = "p"\n}\n')
    (tmp_path / "backend.tf").write_text("terraform {}\n")
    return {p.name: p.read_text() for p in tmp_path.glob("*.tf")}


def test_tidy_splits_files_and_keeps_backend(tmp_path):
    workspace_files(tmp_path)
    result = tidy(FakeWorkspace(tmp_path, clean_after=True), "p")
    assert sorted(p.name for p in tmp_path.glob("*.tf")) == [
        "apis.tf",
        "backend.tf",
        "network.tf",
        "providers.tf",
        "variables.tf",
    ]
    assert "project = var.project" in (tmp_path / "providers.tf").read_text()
    assert result["references"] >= 2


def test_tidy_restores_every_file_if_the_plan_is_no_longer_clean(tmp_path):
    before = workspace_files(tmp_path)
    with pytest.raises(TerraformError, match="no longer plans clean"):
        tidy(FakeWorkspace(tmp_path, clean_after=False), "p")
    assert {p.name: p.read_text() for p in tmp_path.glob("*.tf")} == before
    assert not (tmp_path / ".tidy-backup").exists()
