# GCP IaC agent

Brings hand-built Google Cloud resources under Terraform **without changing them**. Done means
`terraform plan` reports no changes after the import.

## How it works

```
discover ─► scaffold ─► plan ──zero change──► review (human) ──approve──► import ─► verify
                         ▲  │
                         │  └── errors / diff ──► repair (Claude) ──┐
                         └───────────────────────────────────────────┘
```

| Step | Who | What |
|---|---|---|
| discover | `gcloud asset search-all-resources` | Read-only scan of the project. Skips unsupported types and resources labelled `goog-terraform-provisioned` (already in some Terraform state). |
| scaffold | Terraform | Writes `providers.tf` and one `import` block per resource, then `terraform plan -generate-config-out=generated.tf` writes the HCL. |
| plan | Terraform | The judge. Zero change = every resource imported, nothing created, updated or deleted. |
| repair | Claude | Reads the errors and per-attribute diffs (`cloud` vs `config`) and returns exact edits to `generated.tf`. Up to `--max-attempts` rounds; each round sees why the previous one failed. |
| review | You | Shows what will be imported. Nothing touches Terraform state before you approve. |
| import | Terraform | Applies the exact plan file you reviewed (checked by SHA-256), only if it is import-only, then re-plans to confirm zero changes. |

Only the repair step uses a model. Everything else is deterministic.

### Guardrails

- The model can edit only `generated.tf`, through exact-match replacements. Edits are rejected if they add
  `lifecycle`/`ignore_changes` (that would hide drift, not fix it), provisioners, `data`/`module`/`provider`
  blocks, or resources of a type that is not being imported.
- Terraform never runs `apply` except on the reviewed, import-only plan. Importing reads the cloud and
  writes local state, so **read-only GCP credentials are enough for the whole run.**
- If zero change is not reached, the run stops with `NEEDS_HUMAN` and prints the remaining diff.

## Run it

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Terraform 1.5+, `gcloud`.

```sh
# GCP: read-only access is enough (roles/viewer + roles/cloudasset.viewer)
gcloud auth application-default login
gcloud services enable cloudasset.googleapis.com --project MY_PROJECT

# Model: either an Anthropic API key ...
export ANTHROPIC_API_KEY=...
# ... or Claude on Vertex AI in your own GCP project
export ANTHROPIC_VERTEX_PROJECT_ID=MY_VERTEX_PROJECT CLOUD_ML_REGION=global

uv sync
uv run gcp-iac-agent MY_PROJECT --workspace ./out/MY_PROJECT --types compute.googleapis.com/Network compute.googleapis.com/Subnetwork
```

Start with one or two types, then widen. Re-running the same command resumes where it stopped
(state is checkpointed in the workspace). Use a new `--workspace` for a fresh run.

| Option | Default | |
|---|---|---|
| `--types` | all supported | Cloud Asset types to include |
| `--max-attempts` | 3 | repair rounds before handing over to a human |
| `--provider-version` | `>= 6.0` | `hashicorp/google` constraint; the exact version is pinned in `.terraform.lock.hcl` |
| `--replan` | | re-plan after you edited `generated.tf` by hand |
| `--approve SHA` / `--reject` | | decide without the interactive prompt |
| `GCP_IAC_AGENT_MODEL` env | `claude-opus-5-5` | model used for repair |

Supported types: VPC networks, subnetworks, firewall rules, static addresses, storage buckets. Add a type
with one line in `SUPPORTED` in [`discover.py`](src/gcp_iac_agent/discover.py).

If `gcp-iac-agent` fails with `No module named 'gcp_iac_agent'` on macOS, the editable-install `.pth`
file carries the `hidden` flag and Python skips it: `chflags nohidden .venv/lib/python*/site-packages/*.pth`.

## Develop

```sh
uv run pytest        # offline: fake terraform binary, fake model, mocked HTTP
uv run ruff check . && uv run ruff format --check .
```

## Not covered yet

- Run against a real project (needs your GCP project and model credentials).
- Remote state backend (state is local `terraform.tfstate` in the workspace).
- Splitting `generated.tf` into per-service files and replacing hard-coded IDs with references.
