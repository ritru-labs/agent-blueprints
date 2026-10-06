# Infrastructure Migration Agent

> [!WARNING]
> **Archived: reference only.** This branch is no longer maintained and will not be merged. Do not build on it or run its execution adapters.
>
> **Why:** it built services, PostgreSQL checkpoints, Docker runners and execution adapters before the core goal was proven on real fixtures.
>
> **Where the work continues:** [`solutions/iac-agent` on `feat/iac-agent-v0`](https://github.com/ritru-labs/agent-blueprints/tree/feat/iac-agent-v0/solutions/iac-agent). There is one goal: bring hand-built AWS infrastructure under Terraform state with **zero changes**, proven by `terraform plan`. Pulumi output returns in V3 and CloudFormation handoff in V4; see that folder's `docs/SCOPE_AND_ROADMAP.md`.
>
> **Still useful to read:** the Pulumi adapter (V3), the CloudFormation retain-and-release handling (V4), and the bounded-read and partial-coverage reporting ideas.

A governed infrastructure migration agent built on LangGraph.

This solution prepares infrastructure migration packages and implements governed ownership transfer to Pulumi. The LangGraph coordinator invokes focused discovery, assessment, advisory reasoning, migration planning, recovery planning, code generation and validation subgraphs before durable human package review. See [specialist contracts and checkpoint compatibility](docs/specialists.md). Separate execution adapters require exact approvals, resource locks, drift checks, and operation reconciliation.

**Implemented scope:** AWS IPv4 VPCs and subnets, bounded AWS reads, safe CloudFormation parsing and retention/release proposals, typed model review using official documentation, Pulumi TypeScript generation, Docker verification, PostgreSQL checkpoints, protected imports, and CloudFormation change-set adapters. Unsupported configurations remain blocked. Discovery explicitly reports partial coverage. Live cloud/model acceptance and production deployment are pending; the executor admits no adapters by default.

## Prepare without credentials

Requires Python 3.11 or newer. From this directory:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install . --no-deps
.venv/bin/infra-migration prepare \
  --inventory examples/network-inventory.json \
  --resource vpc-fixture --resource subnet-fixture \
  --output generated-project --checkpoint preparation.sqlite
```

This writes the project and pauses for review. Restart with the same arguments and add `--acknowledge-digest DIGEST_FROM_OUTPUT`. Acknowledgement records package review; it cannot enable execution. Without an image or model configuration, their receipts explicitly say they were not configured. After editing source, reinstall the package before testing.

## Isolated compilation

Build the pinned compiler image without customer credentials, resolve its immutable ID, then compile:

```sh
docker build -t infra-migration-compile:local runner
migration_image=$(docker image inspect infra-migration-compile:local --format '{{.Id}}')
.venv/bin/infra-migration compile --project generated-project --runner-image "$migration_image"
```

Compilation mounts the exact bundle read-only, disables network access, drops capabilities, bounds CPU/memory/output/time, and uses baked pinned dependencies. The optional Pulumi runner is built with `runner/Dockerfile.pulumi.amd64` or `.arm64` and `--build-arg COMPILE_IMAGE=IMMUTABLE_COMPILER_ID`; CLI and provider archives have fixed checksums. Preview/import require a separately qualified egress network, an explicit short-lived read credential lease, and a protected destination-state directory. A network name alone does not establish egress enforcement.

## Scoped reads and model review

After an AWS account/profile is explicitly authorized:

```sh
.venv/bin/infra-migration discover --scope examples/scope.json \
  --profile APPROVED_PROFILE --output discovered-inventory.json --max-calls 200
```

The reader verifies the actual account with STS before EC2 reads. Untagged resources have unknown ownership; they are never automatically declared manual. Defaults, unsupported features, missing reads, and budget exhaustion stay visible. A snapshot supplied to `prepare` is operator input, not proof of live discovery.

Optionally add `--model APPROVED_MODEL --official-docs` and an approved HTTPS `--model-base-url` to preparation. Configure credentials through the provider environment, never command arguments or repository files. The model receives resource aliases, types, ownership, dependencies, blockers, and fixed official-document excerpts. It receives no customer configuration, resource tags, account IDs, or cloud credentials. Typed output is advisory; call budgets persist and receipts retain hashes and usage without prompts or hidden reasoning.

## Execution authority

`Executor` has an empty qualified-adapter allowlist. A trusted administrator must independently qualify and admit each exact adapter version. Execution requires a distinct reviewer, a single-use expiring approval bound to artifacts/inventory/plan/state/scope, and an executor identity. It locks physical resources across runs, rechecks drift under lock, journals submission, and retains uncertain outcomes until read-only reconciliation. No CLI command bypasses these gates.

`PulumiImportAdapter` validates protected physical IDs, provider account/region, expected configuration, a zero-change preview, and trusted health observations. `CloudFormationTransferAdapter` separates retention from release, verifies physical identity and freeze evidence, restricts change-set effects, and reconciles observed outcomes. Neither adapter has passed live cloud acceptance. Source re-adoption and universal rollback are not assumed.

The CLI uses OS identity and SQLite for a trusted local operator. PostgreSQL checkpoint support requires a dedicated restricted role and schema per tenant. An authenticated preparation HTTP API, database-backed organization roles, durable PostgreSQL worker queue, and PostgreSQL execution ledger are now implemented. Deploy the credential broker, egress enforcement, encrypted evidence retention, and operational recovery and qualify the full service before production use. Do not expose graph/checkpointer internals, identity constructors, health callbacks, or adapter admission as public request inputs.

## Authenticated service

The service validates pinned issuer/audience/signature/expiry, resolves organization roles from PostgreSQL, queues idempotent preparation and distinct-reviewer jobs, and exports verified artifacts. Uploaded inventory remains an unverified snapshot and cannot authorize execution. See [service provisioning, API and worker commands](docs/service-deployment.md). Cloud execution has no HTTP route.

## Operator dashboard

Open the service root for snapshot upload, resource selection, real run tracking, verified code previews/downloads, and distinct-reviewer decisions. Sessions use memory-only access tokens; browser SSO login remains pending. See [dashboard operation and browser qualification](docs/dashboard.md).

Managed signing-key rotation, cached database readiness and redacted request telemetry are available. See [operational controls and remaining acceptance](docs/operations.md).

## Verify and qualify

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

Integration campaigns opt into real dependencies through `INFRA_RUNNER_IMAGE` and `INFRA_TEST_POSTGRES_DSN`; absent dependencies are skipped, not passed. GitHub CI supplies the pinned compiler image and disposable PostgreSQL. For local integration, `scripts/qualify_local.py --compiler-image IMMUTABLE_ID --postgres-image IMMUTABLE_ID` provisions and removes its own disposable database container. It never contacts AWS or a model provider.

The original `infra-migration-demo` remains a synthetic assessment/control fixture with explicit zero live calls and writes. Terraform, Bicep, Azure, GCP, and broader resource families remain extension targets.

- [Architecture](docs/architecture.md)
- [Tool contracts](docs/tool-contracts.md)
- [Threat model](docs/threat-model.md)
- [Qualification gates](docs/qualification.md)
- [Migration/recovery runbooks](docs/runbooks.md)
- [Operator handoff](docs/operator-handoff.md)
- [Agent governance](AGENTS.md)
