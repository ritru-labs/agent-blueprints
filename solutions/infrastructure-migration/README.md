# LangGraph infrastructure migration blueprint

This blueprint establishes the governed foundation for AWS infrastructure adoption and CloudFormation-to-Pulumi migration. The intended product discovers existing infrastructure, evaluates migration feasibility, generates target code, and transfers management through controlled, verified steps.

**Current capability:** a runnable LangGraph assessment of synthetic resource metadata, application-side tool restrictions, durable local checkpoint recovery, and exact-plan review acknowledgement. Live AWS discovery, language-model reasoning, Pulumi code generation and preview, imports, and ownership transfers are not implemented. Production use is not qualified.

## Run the assessment

Requires Python 3.11 or newer. Run from `solutions/infrastructure-migration`:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install . --no-deps
.venv/bin/infra-migration-demo --checkpoint demo.sqlite --output demo-report.json
```

The first run pauses at `AWAITING_REVIEW`. Restart the process and acknowledge that same assessment:

```sh
.venv/bin/infra-migration-demo --checkpoint demo.sqlite --output demo-report.json --acknowledge
```

The final status is `REVIEWED_BLOCKED`: assessment acknowledged, execution disabled. The JSON report explicitly records zero live model calls, cloud calls, and resource writes. For the CloudFormation fixture use `--workflow cloudformation_migration` with a different checkpoint filename. Both examples contain unsupported resources and partial discovery so gaps remain visible.

SQLite is a local single-operator fixture store. It is not a tenant-isolated production service. The `Principal` class represents trusted authenticated context; the CLI supplies a synthetic principal. Do not expose constructors, the compiled graph, or the checkpointer as public API endpoints.

## Validate changes

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

The suite exercises privileged-tool denial, tenant and account scope, resource identity validation, dependency gaps and cycles, durable restart, stale review decisions, role checks, and prevention of execution claims. These are local foundation tests, not live migration acceptance.

## Design and delivery

- [Architecture and production controls](docs/architecture.md)
- [Tool contracts and permissions](docs/tool-contracts.md)
- [Threat model](docs/threat-model.md)
- [Qualification and implementation roadmap](docs/qualification.md)
- [Migration and recovery runbooks](docs/runbooks.md)
- [Agent instructions](AGENTS.md)

Dependencies are pinned in `requirements.lock`. Review updates intentionally and rerun qualification; adapters must pin their provider schemas and CLI versions as well.

After editing runtime code, reinstall the local package before validation. The test commands exercise the installed package, including its distributable layout.
