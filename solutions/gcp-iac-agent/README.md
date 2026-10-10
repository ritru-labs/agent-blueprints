# GCP IaC agent

Brings hand-built Google Cloud resources under Terraform **without changing them**. Done means
`terraform plan` reports no changes after the import.

## How it works

```
discover ─► scaffold ─► plan ──zero change──► review (human) ──approve──► import ─► verify ─► tidy
                         ▲  │
                         │  └── errors / diff ──► repair (model) ──┐
                         └──────────────────────────────────────────┘
```

| Step | Who | What |
|---|---|---|
| discover | `gcloud` | Read-only scan: Cloud Asset Inventory, plus the router list (Cloud NAT) and the project IAM policy (role grants). Skips, with a reason shown at review: unsupported types, resources already in Terraform (`goog-terraform-provisioned`), subnets of auto-mode networks, Google-created service accounts, secret versions, and human/Google-managed role grants. |
| scaffold | Terraform | Writes `providers.tf`, optional `backend.tf`, one `import` block per resource, then `terraform plan -generate-config-out=generated.tf` writes the HCL. Resources Terraform cannot read (e.g. deleted since Cloud Asset indexed them) are dropped with a reason. Refuses a workspace whose state already manages resources. |
| plan | Terraform | The judge. Zero change = every resource imported, nothing created, updated or deleted. |
| repair | Gemini or Claude | Reads the errors and per-attribute diffs (`cloud` vs `config`) and returns edits, each scoped to one resource block. Up to `--max-attempts` rounds; each round sees why the previous one failed. |
| review | You | Shows what will be imported and what was skipped. Nothing touches Terraform state before you approve. |
| import | Terraform | Applies the exact plan file you reviewed (checked by SHA-256), only if it is import-only, then re-plans to confirm zero changes. |
| tidy | deterministic | `--tidy`: splits into `network.tf`, `compute.tf`, `iam.tf`, `secrets.tf`, `apis.tf`, uses references instead of literal IDs within the state, and `var.project`. Restores every file unless the plan is still zero-change. |

Only the repair step uses a model. Everything else is deterministic.

### Guardrails

- The model can edit only `generated.tf`, one resource block per edit. Edits are rejected if they add
  `lifecycle`/`ignore_changes` (that would hide drift, not fix it), provisioners, `data`/`module`/`provider`
  blocks, or resources of a type that is not being imported.
- Fixed policies the model cannot undo: every `google_project_service` gets `disable_on_destroy = false`, so
  removing an API from Terraform never disables it in GCP. Secret versions (the secret values) are never imported.
- Terraform never runs `apply` except on the reviewed, import-only plan. Importing reads the cloud and
  writes state, so **read-only GCP credentials are enough for the whole run.**
- If zero change is not reached, the run stops with `NEEDS_HUMAN` and prints the remaining diff.

## Run it

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Terraform 1.5+, `gcloud`.

```sh
# GCP: read-only access is enough (roles/viewer + roles/cloudasset.viewer)
gcloud auth login
gcloud services enable cloudasset.googleapis.com --project MY_PROJECT

# Model for the repair step: Gemini (default) ...
export GEMINI_API_KEY=...
# ... or Claude: GCP_IAC_AGENT_PROVIDER=anthropic with ANTHROPIC_API_KEY (or ANTHROPIC_VERTEX_PROJECT_ID)

uv sync
uv run gcp-iac-agent MY_PROJECT --workspace ./out/MY_PROJECT-network --state-bucket MY_STATE_BUCKET \
  --types compute.googleapis.com/Network compute.googleapis.com/Subnetwork compute.googleapis.com/Firewall
# review, approve, then:
uv run gcp-iac-agent MY_PROJECT --workspace ./out/MY_PROJECT-network --tidy
```

Import one area at a time, each in its own `--workspace` (and so its own state). Re-running the same
command resumes where it stopped.

| Option | Default | |
|---|---|---|
| `--types` | all supported | Cloud Asset types to include (see `SUPPORTED` in [`discover.py`](src/gcp_iac_agent/discover.py)) |
| `--state-bucket` | local state | GCS bucket; state goes to `gcp-iac-agent/<workspace name>` |
| `--credentials` | `gcloud` | Terraform reads as the active gcloud login (same identity as discovery); `adc` for CI |
| `--max-attempts` | 3 | repair rounds before handing over to a human |
| `--provider-version` | `>= 6.0` | `hashicorp/google` constraint; the exact version is pinned in `.terraform.lock.hcl` |
| `--replan` | | re-plan after you edited `generated.tf` by hand |
| `--approve SHA` / `--reject` | | decide without the interactive prompt |
| `--tidy` | | after import: split files, references, `var.project` |
| `GCP_IAC_AGENT_PROVIDER` env | `gemini` | `gemini` or `anthropic` |
| `GCP_IAC_AGENT_GEMINI_MODEL` / `GCP_IAC_AGENT_MODEL` env | `gemini-2.5-flash` / `claude-opus-5-5` | repair model |

Supported: networks, subnetworks, firewall rules, static addresses, VMs, disks, resource policies,
routers, Cloud NAT, storage buckets, service accounts, Secret Manager secrets (container only), enabled
APIs, and role grants to the project's own service accounts.

To run Terraform by hand in a workspace with the same identity as the agent:
`GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token) terraform plan`.

If `gcp-iac-agent` fails with `No module named 'gcp_iac_agent'` on macOS, the editable-install `.pth`
file carries the `hidden` flag and Python skips it: `chflags nohidden .venv/lib/python*/site-packages/*.pth`
(or run `PYTHONPATH=src .venv/bin/python -m gcp_iac_agent.cli ...`).

## Develop

```sh
uv run pytest        # offline: fake terraform binary, fake model, mocked HTTP
uv run ruff check . && uv run ruff format --check .
```

## Not covered yet

- References between separate states (e.g. the VM's network in the networking state) stay literal IDs.
- Billing-account resources (budgets), organization policies, human IAM access.
- GKE, Cloud SQL, Cloud Run and other services: add a row to `SUPPORTED`, then run one area at a time.
