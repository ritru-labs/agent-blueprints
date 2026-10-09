"""Read-only discovery of a GCP project through Cloud Asset Inventory."""

import json
import re
import subprocess
from dataclasses import asdict, dataclass

# Cloud NAT lives inside a router and is not a Cloud Asset type; it is read from the router list.
ROUTER_NAT = "compute.googleapis.com/RouterNat"

# Cloud Asset type -> Terraform resource type. Add a row to support a new type;
# the repair loop absorbs most provider quirks, so a row is usually all it takes.
SUPPORTED = {
    "compute.googleapis.com/Network": "google_compute_network",
    "compute.googleapis.com/Subnetwork": "google_compute_subnetwork",
    "compute.googleapis.com/Firewall": "google_compute_firewall",
    "compute.googleapis.com/Address": "google_compute_address",
    "compute.googleapis.com/Instance": "google_compute_instance",
    "compute.googleapis.com/Disk": "google_compute_disk",
    "compute.googleapis.com/ResourcePolicy": "google_compute_resource_policy",
    "compute.googleapis.com/Router": "google_compute_router",
    ROUTER_NAT: "google_compute_router_nat",
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
    if asset_type == ROUTER_NAT:  # Terraform wants .../routers/ROUTER/NAT, without "nats/"
        return asset_name.split(".googleapis.com/", 1)[1].replace("/nats/", "/")
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


def router_nats(routers: list[dict]) -> list[dict]:
    """Cloud NAT configs as asset-shaped records, so they flow through the same mapping as real assets."""
    return [
        {
            "assetType": ROUTER_NAT,
            "name": f"//compute.googleapis.com/{router['selfLink'].split('/compute/v1/', 1)[1]}/nats/{nat['name']}",
        }
        for router in routers
        for nat in router.get("nats", [])
    ]


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
    searchable = [t for t in asset_types if t != ROUTER_NAT]
    assets = []
    if searchable:
        assets = gcloud_json(
            "asset",
            "search-all-resources",
            f"--scope=projects/{project}",
            f"--asset-types={','.join(searchable)}",
        )
    if ROUTER_NAT in asset_types:
        assets += router_nats(gcloud_json("compute", "routers", "list", f"--project={project}"))
    owned = {}
    if "compute.googleapis.com/Subnetwork" in asset_types:
        owned = auto_subnets(gcloud_json("compute", "networks", "list", f"--project={project}"))
    return parse_assets(assets, owned)
