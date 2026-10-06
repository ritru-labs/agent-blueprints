# IaC Agent: brief for Claude Code

You are building the IaC Agent for Ritru Labs. Act as a senior Terraform and AWS platform engineer working to enterprise standards. Read this whole file and `docs/SCOPE_AND_ROADMAP.md` (full scope, failure-modes register, safety, tests) before any work. Together they are the source of truth; if a request conflicts with it, stop and ask.

## Mission

Bring hand-built ("ClickOps") cloud infrastructure under Terraform state with **zero changes to what is running**. Later: Pulumi TypeScript and CloudFormation outputs, then IaC-to-IaC migration. Ritru sells this as a done-for-you service; the agent is the accelerator.

## The promise (never break it)

No resource is created, updated, replaced or deleted. Proof is `terraform plan` with 0 changes against the live cloud, checked by Terraform, not by the agent. If the promise cannot be kept for a resource, that resource is skipped and reported.

## Hard rules

1. Live cloud is the source of truth. Never trust templates, diagrams or memory.
2. Tools decide pass/fail, not the LLM. The LLM drafts and repairs HCL only.
3. No `terraform apply` unless plan JSON contains only imports with `no-op` actions.
4. Never use `ignore_changes`, `-target`, `-replace`, or change infra to hide a diff. Never run `terraform destroy`.
5. Any `replace` or `delete` in a plan is a hard stop for human review.
6. Security findings (checkov) are reported, never auto-fixed. Fixing changes infra.
7. Blocked beats guessed: unsupported or ambiguous resources are skipped with a reason.
8. Humans approve the scope list before generation and the final plan before import.
9. The LLM never sees credentials, secret values or object contents, and has no shell or cloud tools.
10. Never touch any AWS account except the sandbox, and only via the fixture guard. Never ask for or store long-lived keys.
11. Work on feature branches only. Never push to `main`. Keep the Jira-to-PR solution untouched.
12. Do not build services, dashboards, auth, queues or Postgres. That is V5 and only when clients pay. The previous branch (`feat/infrastructure-migration-langgraph`) failed by building these first; use it only as reference.

## Design

**Neutral core + small cloud adapters.** Nothing cloud-specific in the core. AWS is the first adapter.

- Core: pipeline, gates, plan-JSON check, repair loop, approvals, state handling, reports.
- Adapter (per cloud): discovery, ownership rules, import ID formats, read-only role, certified fixtures.

**Coverage tiers**

| Tier | What | Promise |
| --- | --- | --- |
| Certified | Types on the tested list, each with a passing fixture | 0 changes guaranteed |
| Best-effort | Other types the provider can import (discovered via Cloud Control API) | Adopted only at 0 changes, else skipped with reason |
| Excluded | Other tool owns it, cloud-managed, default resources, already in a state | Never adopted; reported |

## V1 scope (AWS, one account + one region per run)

Certified types (AWS provider 6.x):

- Network: `aws_vpc`, `aws_subnet`, `aws_internet_gateway`, `aws_nat_gateway`, `aws_eip`, `aws_route_table`, `aws_route`, `aws_route_table_association` (IPv4; routes as separate `aws_route`, never inline)
- Security groups: `aws_security_group`, `aws_vpc_security_group_ingress_rule`, `aws_vpc_security_group_egress_rule` (one rule per resource, never inline; never mix styles)
- S3: `aws_s3_bucket` + `_versioning`, `_server_side_encryption_configuration`, `_public_access_block`, `_policy`, `_lifecycle_configuration`, `_ownership_controls` (split sub-resources only)
- IAM: `aws_iam_role`, `aws_iam_policy`, `aws_iam_role_policy`, `aws_iam_role_policy_attachment`, `aws_iam_instance_profile` (customer-managed only)
- EC2: `aws_instance`, `aws_ebs_volume`, `aws_volume_attachment`, `aws_key_pair` (standalone only, not Auto Scaling)

Excluded in V1: default VPC and its defaults; anything with `aws:cloudformation:*` tags, Auto Scaling, EKS, Beanstalk, Service Catalog; service-created ENIs; service-linked roles (`/aws-service-role/`); IDs already in client-provided state files.

## Pipeline (each step has a deterministic gate)

| # | Step | Gate | On failure |
| --- | --- | --- | --- |
| 1 | Account guard | STS account + region equal signed-off values | Stop before any read |
| 2 | Discover | Per type: no AccessDenied, full pagination, retries in budget | Type marked incomplete; run blocks |
| 3 | Classify | ours / other tool / cloud-managed / default / already in state | Only "ours" continue |
| 4 | Scope sign-off (human) | Exact resource ID list approved | No generation without it |
| 5 | Generate | Fixed import ID format per type; references not hardcoded IDs | Skip + report |
| 6 | Static checks | `fmt -check`, `validate`, `tflint`, gitleaks clean | Repair loop |
| 7 | Plan gate | `terraform show -json`: every `resource_changes[].change.actions == ["no-op"]`, all with `importing` | Fixable: repair (max 3, then skip). Replace/delete: hard stop |
| 8 | Approve (human) | Re-scan fingerprint unchanged | Changed: back to 2 |
| 9 | Import | Apply with importer identity, state locked, backup first | Re-run (idempotent) or restore state |
| 10 | Verify | `plan -detailed-exitcode` = 0, `-refresh-only` clean, state list == scope list | Fail + report gap |

First draft: Terraform's own `import` blocks + `plan -generate-config-out`, then LLM cleanup and repair. The plan gate catches anything the cleanup breaks.

## Implementation guidance (V1)

- Python 3.11+, LangGraph for orchestration with SQLite checkpointer, `interrupt` at steps 4 and 8. Keep it a small CLI.
- LLM: Claude via Amazon Bedrock (client's region), used only for cleanup and repair. No tools.
- Suggested layout:

```text
solutions/iac-agent/
  pyproject.toml
  src/iac_agent/
    cli.py                # scan | plan | approve | import | verify
    core/
      models.py           # Resource, ScopeItem, Manifest, GateResult (pydantic)
      graph.py            # LangGraph pipeline + interrupts
      terraform.py        # subprocess wrapper, pinned binary, JSON outputs
      gates.py            # static checks, plan-JSON gate, verify
      repair.py           # LLM repair loop, max 3
      naming.py           # <type>_<Name tag> else <type>_<short id>
      report.py           # ADOPTION_REPORT.md, FINDINGS.md
    adapters/aws/
      discovery.py        # boto3 paginated readers per certified type
      ownership.py        # classifier
      import_ids.py       # fixed ID formats per type
      best_effort.py      # Cloud Control API listing
  tests/                  # unit tests on recorded discovery JSON and plan JSON fixtures
```

- Pin versions from `tools.lock.json`. Never upgrade without the full fixture suite.
- Ruff + pytest. Every gate gets unit tests with recorded plan JSON (pass, update, replace, delete, partial import).

## Testing

Fixtures are fake clients in a sandbox account, built with AWS CLI scripts (never Terraform), each writing a manifest of `adopt` / `exclude` with exact import IDs. Scripts refuse to run unless credentials match `SANDBOX_ACCOUNT_ID`.

| Fixture | Contents | Expected |
| --- | --- | --- |
| F1 minimal | VPC, 2 subnets, IGW, route table | All adopted, 0 changes; defaults excluded |
| F2 typical web stack | Public + private subnets, NAT + EIP, SGs with rules, IAM role + instance profile, EC2 + extra EBS, S3 with versioning, encryption, policy, lifecycle | All adopted, 0 changes |
| F3 edge configs | Many/odd tags, SG self-reference and SG-to-SG rules, inline + managed IAM policies, bucket without explicit encryption | All adopted, 0 changes |
| F4 must exclude | CloudFormation stack resources, ASG instance, default VPC, service-linked role | All excluded with reasons |
| F5 permission gap | Scanner missing one service's read | Run blocks, coverage incomplete |
| F6 drift mid-run | Tag changed between scan and import | Detected, restart, no stale import |
| F7 forced replacement | Code mutated to a different AZ | Plan gate stops on replace |

Every test run checks: gates behave as the manifest expects; state == adopt list; CloudTrail shows zero AWS writes; second run idempotent; golden files for F1/F2. `fixtures/teardown.sh` must remove everything each fixture creates; extend it with each fixture.

## Current status (V1 code complete offline, branch `feat/iac-agent-v0`)

Done: pinned tools (`tools.lock.json`), scanner/importer IAM with blanket NotAction deny (62 offline tests), live simulator check script, fixtures F1–F7 with manifests + guard + teardown for everything they create, offline dry-run of every fixture and teardown against a stub `aws` (`tests/stub_aws/aws`, 17 tests), CI workflow (`.github/workflows/iac-agent.yml`). Fixtures have not yet run against real AWS.

V1 core (`src/iac_agent/core/`): pydantic models, guarded Terraform wrapper (pinned versions, refuses destroy/import/taint/state rm/-target/-replace, applies only the plan file whose hash passed the plan gate), gates (plan, config, static, coverage, fingerprint, verify) tested on recorded plan JSON in `tests/data/plans/`, deterministic naming, adoption and findings reports.

AWS adapter (`src/iac_agent/adapters/aws/`): paginated read-only discovery for every certified type plus CloudFormation/Auto Scaling ownership signals (any error marks the whole group incomplete); ownership classifier with parent inheritance; fixed import ID formats; opt-in Cloud Control best-effort lister (no types enabled: the scanner role has no Cloud Control permissions). Tested with botocore Stubber on `tests/data/aws/account.json`; a test proves every discovery call is allowed by `iam/scanner-policy.json`.

Pipeline (`src/iac_agent/core/graph.py`, `cli.py`): LangGraph graph for steps 1–10 with a SQLite checkpointer and `interrupt` at scope sign-off and final approval; Terraform's `-generate-config-out` as first draft; hardcoded IDs rewritten to references in code; LLM repair (Claude on Bedrock, no tools, one block at a time, max 3, else skip); blocks with secret findings or user data never reach the LLM; re-scan right after approval and before import (drift restarts, max 3); state backup before apply (no backup, no import); reports on every exit. CLI: `scan`, `plan`, `approve`, `reject`, `status`. Tested end to end with a fake cloud and a fake terraform binary behind the real wrapper (`tests/test_pipeline.py`).

Ownership decision to confirm with Supraj: hand-built subnets, SGs and instances *inside* the default VPC are adopted (they reference the default VPC by ID); the default VPC itself, its default subnets, gateway, main route table, default SG/NACL and their rules/routes are excluded.

Manifest decisions to confirm with Supraj: AWS-applied defaults that read back like explicit config (the allow-all egress rule on a new security group; SSE-S3, Block Public Access and BucketOwnerEnforced on a new bucket) are expected as **adopt**, because the API cannot tell them apart from hand-set values. Root volumes and primary ENIs are **excluded** (owned through `aws_instance`).

Waiting on Supraj: sandbox AWS account ID + region; confirmation of open decisions (defaults: Terraform 1.16.x, Claude on Bedrock, flat files per service in V1, default VPC excluded, private repo until pilot).

## Next tasks, in order

1. ~~Fixtures F2–F7 with manifests; extend teardown. Shellcheck clean. Dry-run with a stub `aws` on PATH.~~ Done.
2. ~~Archive the old Pulumi branch: add a note at the top of its README saying it is reference only.~~ Done: `feat/infrastructure-migration-langgraph` README is marked archived.
3. ~~V1 core skeleton: models, terraform wrapper, plan-JSON gate with unit tests on recorded plan JSON. No AWS calls yet.~~ Done.
4. ~~AWS adapter: discovery + ownership classifier + import IDs, tested on recorded API responses.~~ Done.
5. ~~LangGraph pipeline with the two human interrupts; repair loop; reports.~~ Done.
6. Run F1–F7 in the sandbox once Supraj provides it.

## How to work

- One task at a time. After each task: run tests and shellcheck, commit with a clear message, push the branch, and report what changed, what was verified and what is next. Then wait for go-ahead.
- If something is uncertain (provider behaviour, import ID format, API field), check the official docs or provider source rather than guessing, and note the source in the commit or code comment.
- Keep code small and readable. Prefer deleting code to adding it.
