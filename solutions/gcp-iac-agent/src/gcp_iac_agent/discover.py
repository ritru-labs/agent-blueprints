"""Read-only discovery of a GCP project through Cloud Asset Inventory."""

import json
import re
import subprocess
from dataclasses import asdict, dataclass

# Cloud Asset type -> Terraform resource type. Add a row to support a new type;
# the repair loop absorbs most provider quirks, so a row is usually all it takes.
SUPPORTED = {
    "compute.googleapis.com/Network": "google_compute_network",
    "compute.googleapis.com/Subnetwork": "google_compute_subnetwork",
    "compute.googleapis.com/Firewall": "google_compute_firewall",
    "compute.googleapis.com/Address": "google_compute_address",
    "storage.googleapis.com/Bucket": "google_storage_bucket",
}

# Set by the Google provider on resources it creates: already owned by some Terraform state.
TERRAFORM_LABEL = "goog-terraform-provisioned"


@dataclass(frozen=True)
class Resource:
    asset_type: str
    tf_type: str
    tf_name: str
    import_id: str

    @property
    def address(self) -> str:
        return f"{self.tf_type}.{self.tf_name}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Discovery:
    resources: list[Resource]
    skipped: list[str]  # human-readable reasons, shown at review


def import_id(asset_type: str, asset_name: str) -> str:
    # Asset names look like //compute.googleapis.com/projects/p/global/networks/n.
    if asset_type == "storage.googleapis.com/Bucket":
        return asset_name.rsplit("/", 1)[-1]
    return asset_name.split(".googleapis.com/", 1)[1]


def tf_name(asset_name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", asset_name.rsplit("/", 1)[-1]).lower() or "resource"
    if not base[0].isalpha():
        base = "r_" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def auto_subnets(networks: list[dict]) -> dict[str, str]:
    """Subnets GCP creates for auto-mode networks: import id -> owning network name.

    They are managed through their network (auto_create_subnetworks = true), not as separate resources.
    """
    owned = {}
    for network in networks:
        if network.get("autoCreateSubnetworks"):
            for link in network.get("subnetworks", []):
                owned[link.split("/compute/v1/", 1)[-1]] = network["name"]
    return owned


def parse_assets(assets: list[dict], auto_owned: dict[str, str] | None = None) -> Discovery:
    resources, skipped, names = [], [], {}
    auto_owned = auto_owned or {}
    for asset in sorted(assets, key=lambda a: a["name"]):
        kind, name = asset.get("assetType", ""), asset["name"]
        if kind not in SUPPORTED:
            skipped.append(f"{name}: type {kind} not supported yet")
            continue
        if TERRAFORM_LABEL in (asset.get("labels") or {}):
            skipped.append(f"{name}: already managed by Terraform ({TERRAFORM_LABEL} label)")
            continue
        if import_id(kind, name) in auto_owned:
            skipped.append(f"{name}: auto-created by auto-mode network {auto_owned[import_id(kind, name)]}")
            continue
        tf_type = SUPPORTED[kind]
        resources.append(
            Resource(kind, tf_type, tf_name(name, names.setdefault(tf_type, set())), import_id(kind, name))
        )
    return Discovery(resources, skipped)


def gcloud_json(*args: str) -> list[dict]:
    result = subprocess.run(
        ["gcloud", *args, "--format=json"], capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"gcloud {args[0]} {args[1]} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def discover(project: str, asset_types: list[str] | None = None) -> Discovery:
    asset_types = asset_types or sorted(SUPPORTED)
    assets = gcloud_json(
        "asset",
        "search-all-resources",
        f"--scope=projects/{project}",
        f"--asset-types={','.join(asset_types)}",
    )
    owned = {}
    if "compute.googleapis.com/Subnetwork" in asset_types:
        owned = auto_subnets(gcloud_json("compute", "networks", "list", f"--project={project}"))
    return parse_assets(assets, owned)
