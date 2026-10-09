from gcp_iac_agent.discover import auto_subnets, parse_assets, project_iam_grants, router_nats

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


def test_subnets_of_auto_mode_networks_are_skipped_not_imported():
    networks = [
        {
            "name": "default",
            "autoCreateSubnetworks": True,
            "subnetworks": [
                "https://www.googleapis.com/compute/v1/projects/p/regions/europe-west1/subnetworks/app"
            ],
        },
        {
            "name": "prod-vpc",
            "autoCreateSubnetworks": False,
            "subnetworks": [
                "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1/subnetworks/app"
            ],
        },
    ]
    found = parse_assets(ASSETS, auto_subnets(networks))
    subnets = {r.import_id for r in found.resources if r.tf_type == "google_compute_subnetwork"}
    assert subnets == {"projects/p/regions/us-central1/subnetworks/app"}
    assert any("auto-created by auto-mode network default" in s for s in found.skipped)


def test_cloud_nat_is_read_from_routers_with_terraform_import_id():
    routers = [
        {
            "selfLink": "https://www.googleapis.com/compute/v1/projects/p/regions/asia-south1/routers/r1",
            "nats": [{"name": "nat-1"}],
        },
        {"selfLink": "https://www.googleapis.com/compute/v1/projects/p/regions/asia-south1/routers/r2"},
    ]
    found = parse_assets(router_nats(routers))
    assert [(r.address, r.import_id) for r in found.resources] == [
        ("google_compute_router_nat.nat_1", "projects/p/regions/asia-south1/routers/r1/nat-1")
    ]


def test_service_accounts_and_secrets_never_include_secret_values_or_google_defaults():
    found = parse_assets(
        [
            {
                "assetType": "iam.googleapis.com/ServiceAccount",
                "name": "//iam.googleapis.com/projects/p/serviceAccounts/ci-vm@p.iam.gserviceaccount.com",
            },
            {
                "assetType": "iam.googleapis.com/ServiceAccount",
                "name": "//iam.googleapis.com/projects/p/serviceAccounts/123-compute@developer.gserviceaccount.com",
            },
            {
                "assetType": "secretmanager.googleapis.com/Secret",
                "name": "//secretmanager.googleapis.com/projects/p/secrets/admin-password",
            },
            {
                "assetType": "secretmanager.googleapis.com/SecretVersion",
                "name": "//secretmanager.googleapis.com/projects/p/secrets/admin-password/versions/1",
            },
        ]
    )
    assert [(r.address, r.import_id) for r in found.resources] == [
        ("google_service_account.ci_vm", "projects/p/serviceAccounts/ci-vm@p.iam.gserviceaccount.com"),
        ("google_secret_manager_secret.admin_password", "projects/p/secrets/admin-password"),
    ]
    assert any("Google-created default service account" in s for s in found.skipped)
    assert any("secret values are never imported" in s for s in found.skipped)


def test_only_grants_to_project_service_accounts_are_imported():
    policy = {
        "bindings": [
            {
                "role": "roles/logging.logWriter",
                "members": ["serviceAccount:ci-vm@p.iam.gserviceaccount.com"],
            },
            {"role": "roles/owner", "members": ["user:someone@example.com"]},
            {"role": "roles/editor", "members": ["serviceAccount:123-compute@developer.gserviceaccount.com"]},
            {
                "role": "roles/viewer",
                "members": ["serviceAccount:ci-vm@p.iam.gserviceaccount.com"],
                "condition": {"title": "temporary"},
            },
        ]
    }
    grants, skipped = project_iam_grants("p", policy)
    found = parse_assets(grants)
    assert [(r.address, r.import_id) for r in found.resources] == [
        (
            "google_project_iam_member.ci_vm_logging_logwriter",
            "p roles/logging.logWriter serviceAccount:ci-vm@p.iam.gserviceaccount.com",
        )
    ]
    assert len(skipped) == 3
    assert any("human or group access" in s for s in skipped)
    assert any("Google-managed service account" in s for s in skipped)
    assert any("conditional grant" in s for s in skipped)
