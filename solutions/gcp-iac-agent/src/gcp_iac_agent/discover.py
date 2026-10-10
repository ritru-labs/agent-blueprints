"""Read-only discovery of a GCP project through Cloud Asset Inventory."""

import json
import re
import subprocess
from dataclasses import asdict, dataclass

# Cloud NAT lives inside a router and is not a Cloud Asset type; it is read from the router list.
ROUTER_NAT = "compute.googleapis.com/RouterNat"
SERVICE = "serviceusage.googleapis.com/Service"
# Project IAM grants are not Cloud Asset resources either; they are read from the project's IAM policy.
PROJECT_IAM = "cloudresourcemanager.googleapis.com/ProjectIamMember"
DERIVED = {ROUTER_NAT, PROJECT_IAM}

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
    # One grant per resource (non-authoritative): never rewrites the project's whole IAM policy.
    PROJECT_IAM: "google_project_iam_member",
    "storage.googleapis.com/Bucket": "google_storage_bucket",
    "iam.googleapis.com/ServiceAccount": "google_service_account",
    # The secret container only (name, replication, labels). Secret versions hold the actual values and
    # are never imported: they would be written in plain text to generated.tf and Terraform state.
    "secretmanager.googleapis.com/Secret": "google_secret_manager_secret",
    # Enabled APIs. The workspace always sets disable_on_destroy = false on these, so removing one
    # from Terraform later never disables the API in GCP.
    SERVICE: "google_project_service",
}

# Created and relied on by Google itself; deleting them through Terraform would break project defaults.
GOOGLE_DEFAULT_ACCOUNT = re.compile(r"/serviceAccounts/[^/]*@(developer|appspot)\.gserviceaccount\.com$")

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
    if asset_type == SERVICE:  # Terraform wants PROJECT/SERVICE
        _, project, _, service = asset_name.split(".googleapis.com/", 1)[1].split("/")
        return f"{project}/{service}"
    if asset_type == ROUTER_NAT:  # Terraform wants .../routers/ROUTER/NAT, without "nats/"
        return asset_name.split(".googleapis.com/", 1)[1].replace("/nats/", "/")
    return asset_name.split(".googleapis.com/", 1)[1]


def tf_name(asset_name: str, taken: set[str]) -> str:
    last = asset_name.rsplit("/", 1)[-1].split("@", 1)[0]  # service account email -> its local part
    last = last.removesuffix(".googleapis.com")  # API name -> its short name
    base = re.sub(r"[^a-zA-Z0-9_]", "_", last).lower() or "resource"
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


def project_iam_grants(project: str, policy: dict) -> tuple[list[dict], list[str]]:
    """Grants to service accounts created in this project, as asset-shaped records.

    Human, group and Google-managed grants are skipped: a mistake there can lock people or Google out.
    """
    grants, skipped = [], []
    own = f"@{project}.iam.gserviceaccount.com"
    for binding in policy.get("bindings", []):
        role = binding["role"]
        for member in binding["members"]:
            if "condition" in binding:
                reason = "conditional grant, not managed yet"
            elif not (member.startswith("serviceAccount:") and member.endswith(own)):
                reason = (
                    "human or group access, not managed by default"
                    if member.split(":", 1)[0] in ("user", "group", "domain")
                    else "Google-managed service account"
                )
            else:
                local = member.split(":", 1)[1].split("@", 1)[0]
                grants.append(
                    {
                        "assetType": PROJECT_IAM,
                        "name": f"//cloudresourcemanager.googleapis.com/projects/{project}/iamMembers/"
                        f"{local}_{role.rsplit('/', 1)[-1]}",
                        "importId": f"{project} {role} {member}",
                    }
                )
                continue
            skipped.append(f"{role} for {member}: {reason}")
    return grants, skipped


def parse_assets(assets: list[dict], auto_owned: dict[str, str] | None = None) -> Discovery:
    resources, skipped, names = [], [], {}
    auto_owned = auto_owned or {}
    for asset in sorted(assets, key=lambda a: a["name"]):
        kind, name = asset.get("assetType", ""), asset["name"]
        if kind == "secretmanager.googleapis.com/SecretVersion":
            skipped.append(f"{name}: secret values are never imported into Terraform")
            continue
        if kind not in SUPPORTED:
            skipped.append(f"{name}: type {kind} not supported yet")
            continue
        if TERRAFORM_LABEL in (asset.get("labels") or {}):
            skipped.append(f"{name}: already managed by Terraform ({TERRAFORM_LABEL} label)")
            continue
        if GOOGLE_DEFAULT_ACCOUNT.search(name):
            skipped.append(f"{name}: Google-created default service account")
            continue
        if import_id(kind, name) in auto_owned:
            skipped.append(f"{name}: auto-created by auto-mode network {auto_owned[import_id(kind, name)]}")
            continue
        tf_type = SUPPORTED[kind]
        resources.append(
            Resource(
                kind,
                tf_type,
                tf_name(name, names.setdefault(tf_type, set())),
                asset.get("importId") or import_id(kind, name),
            )
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
    searchable = [t for t in asset_types if t not in DERIVED]
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
    if assets:
        # Some services name assets by project number; use the project ID so the config reads naturally.
        number = gcloud_json("projects", "describe", project)["projectNumber"]
        assets = [
            {**a, "name": a["name"].replace(f"/projects/{number}/", f"/projects/{project}/")} for a in assets
        ]
    owned = {}
    if "compute.googleapis.com/Subnetwork" in asset_types:
        owned = auto_subnets(gcloud_json("compute", "networks", "list", f"--project={project}"))
    iam_skipped = []
    if PROJECT_IAM in asset_types:
        grants, iam_skipped = project_iam_grants(project, gcloud_json("projects", "get-iam-policy", project))
        assets += grants
    found = parse_assets(assets, owned)
    return Discovery(found.resources, found.skipped + iam_skipped)
