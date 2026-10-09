from gcp_iac_agent.discover import parse_assets

ASSETS = [
    {
        "name": "//compute.googleapis.com/projects/p/global/networks/prod-vpc",
        "assetType": "compute.googleapis.com/Network",
    },
    {
        "name": "//compute.googleapis.com/projects/p/regions/us-central1/subnetworks/app",
        "assetType": "compute.googleapis.com/Subnetwork",
    },
    {
        "name": "//compute.googleapis.com/projects/p/regions/europe-west1/subnetworks/app",
        "assetType": "compute.googleapis.com/Subnetwork",
    },
    {"name": "//storage.googleapis.com/9-logs.example", "assetType": "storage.googleapis.com/Bucket"},
    {
        "name": "//compute.googleapis.com/projects/p/global/networks/tf-made",
        "assetType": "compute.googleapis.com/Network",
        "labels": {"goog-terraform-provisioned": "true"},
    },
    {
        "name": "//sqladmin.googleapis.com/projects/p/instances/db",
        "assetType": "sqladmin.googleapis.com/Instance",
    },
]


def test_maps_supported_assets_to_import_blocks():
    found = parse_assets(ASSETS)
    by_address = {r.address: r.import_id for r in found.resources}
    assert by_address == {
        "google_compute_network.prod_vpc": "projects/p/global/networks/prod-vpc",
        "google_compute_subnetwork.app": "projects/p/regions/europe-west1/subnetworks/app",
        "google_compute_subnetwork.app_2": "projects/p/regions/us-central1/subnetworks/app",
        "google_storage_bucket.r_9_logs_example": "9-logs.example",
    }


def test_skips_terraform_owned_and_unsupported_resources_with_reasons():
    skipped = parse_assets(ASSETS).skipped
    assert any("tf-made" in s and "already managed by Terraform" in s for s in skipped)
    assert any("sqladmin" in s and "not supported" in s for s in skipped)
