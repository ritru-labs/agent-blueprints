# Tool contracts and permissions

The tool gateway is an application boundary. The fixture gateway accepts only `read_inventory` on synthetic provenance. The separate AWS reader requires an explicit session and scope, verifies STS account identity, and bounds read calls. Known privileged tools fail closed; unknown names and extra schema fields are rejected. There is no arbitrary shell tool or model-generated permission flag.

## Current contracts

`ToolRequest` binds the tool name, exact tenant/run/cloud/account/regions/workflow/target scope, and bounded request ID. `Principal` is trusted authentication context. `Inventory` includes typed configuration, blockers, and explicit synthetic or AWS API provenance. Responses have an output digest receipt. This local receipt is not a signed production execution record.

All current fixture reads must match the bound inventory scope and assessor role. Cloud writes are denied even if a caller acknowledges a plan. Docker runners allow only enumerated compiler/Pulumi commands, immutable image IDs, bounded output, and explicitly leased credentials for read-only preview. The optional model reviewer receives aliases and type metadata, never cloud credentials or customer configuration. SQLite stores local graph state; users with filesystem access can change it, so it is unsuitable as a hostile multi-tenant security boundary.

## Tool registry and integration contracts

| Tool | Input binding | Result | Permission and gate |
| --- | --- | --- | --- |
| discover_resources | Approved scope, service list, pagination budget | Inventory, coverage, evidence | Cloud read; no discovery setup writes |
| describe_configuration | Resource identities, fields, secret policy | Configuration and provenance | Scoped read with redaction |
| read_source | Repository and immutable commit | Parsed templates and dependencies | Repository read |
| fetch_official_docs | Allowlisted URL, version, size budget | Sanitized document and content hash | Restricted retrieval; SSRF and redirect checks |
| lookup_provider_schema | Pinned provider and resource type | Property and import schema | Verified registry metadata |
| propose_mapping | Canonical model and schema references | Typed mapping proposal | Untrusted model output; verifier required |
| generate_project | Accepted mapping, output directory, budget | Candidate code and manifest | Isolated workspace write |
| validate_project | Exact artifact tree and pinned environment | Compile, security and parity results | Isolated runner; no production credentials |
| preview_migration | Verified code, destination, scoped read identity | Structured diff and runner receipt | Read credential; executable code sandbox |
| import_batch | Immutable manifest, approval ID, lock fence | Operation receipts and resulting state | Separate state-write approval |
| release_source_ownership | Approved source changes and retain proof | Source receipts and ownership evidence | Cloud write; separate approval |
| verify_migration | Resource IDs, baseline, expected ownership | Configuration, preview and health evidence | Scoped reads |
| reconcile_operation | Journal operation ID and expected scope | Verified observed outcome | Read-only reconciliation |

## Production request and response envelope

The trusted gateway appends principal identity, tenant, run, scope, artifact digests, policy version, deadline, output budget, correlation ID, and operation classification. Tool code cannot override these fields through model arguments. Privileged operations additionally require authenticated approval references and lock fencing. Validate both request and response schemas and cross-check actual provider identity before every operation.

Persist redacted invocation and result receipts outside the model context. An execution receipt binds request digest, operation ID, adapter digest, timestamps, provider IDs, exit status, output artifacts, and observed outcome. Reject mismatched, missing, stale, or duplicated evidence. Do not classify exit code zero alone as criterion satisfaction.

Transport retries may be used for bounded safe reads. Writes require provider idempotency or reconciliation; a timeout is not evidence that nothing happened. Support `OUTCOME_UNKNOWN` explicitly. Use subprocess argument arrays in runners, never interpolated shell text from prompts.

Implemented modules: `discovery` (AWS reads), `source` (safe bounded template parsing), `reasoning` (fixed official URLs and typed model review), `generation` (VPC/subnet code and manifests), `runner` (compile/export/import/preview), `execution` and `ledger` (approval, locks, journal), `pulumi_adapter` (protected import and parity), `cloudformation_adapter` (retention/release change sets), and `credentials` (scoped read leases). Provider mappings are pinned in code; generic runtime schema lookup and arbitrary framework conversion are not implemented. Adapter callbacks and qualified-adapter configuration belong to trusted services, never public tool arguments.
