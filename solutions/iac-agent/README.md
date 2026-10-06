# IaC Agent

Brings hand-built cloud infrastructure under Terraform state with **zero changes to what is running**. Later versions add Pulumi TypeScript and CloudFormation outputs, and IaC-to-IaC migration.

> **The promise:** no resource is created, updated, replaced or deleted. Proof is a `terraform plan` with 0 changes against the live cloud, checked by Terraform, not by the agent.

Status: **V0 (foundations)**. No agent code yet. Nothing here touches a client account.

## Design in one minute

- **Live cloud is the source of truth.** Templates and memory are not.
- **Tools decide, not the AI.** The LLM drafts and repairs HCL; `validate`, plan JSON and state checks decide pass or fail.
- **Read-only by design.** Both agent identities are denied every AWS write, even if another policy is attached.
- **Blocked beats guessed.** Unsupported or ambiguous resources are skipped and reported.
- **Humans approve** the scope list and the final plan.
- **Neutral core, small cloud adapters.** AWS is the first adapter, not a special case.

Coverage tiers: **certified** types (tested fixture, guaranteed), **best-effort** types (adopted only at 0 changes, else skipped), **excluded** (other tool, cloud-managed, defaults, already in a state).

## Roadmap

Full scope, failure-modes register, safety rules and test plan: [`docs/SCOPE_AND_ROADMAP.md`](docs/SCOPE_AND_ROADMAP.md).

| Version | Scope | Exit gate |
| --- | --- | --- |
| V0 | Sandbox, read-only IAM, pinned tools, fixtures F1–F7 | IAM proven unable to write; fixtures build and tear down |
| V1 | Console → Terraform on AWS: network, SGs, S3, IAM, EC2 | F1–F7 pass in CI; 0 changes on a real pilot |
| V2 | More AWS types, multi-region, multi-account, modules | Each new type passes its fixture |
| V3 | Pulumi TypeScript and CloudFormation outputs | F1–F3 at 0 changes per language |
| V4 | Migrate mode with safe ownership handoff | Every path passes, including a failure mid-handoff |
| V5 | Drift checks, PR workflow, service | Paying demand exists |
| Adapters | Azure, GCP, OCI | Own certified fixtures at 0 changes |

## What is here (V0)

| Path | Purpose |
| --- | --- |
| `tools.lock.json` | Exact tool versions for every run |
| `iam/scanner-policy.json` | Read-only discovery identity; explicit deny on everything else, including object and secret reads |
| `iam/importer-policy.template.json` | Same reads plus Terraform state bucket and key only |
| `iam/trust-policy.template.json` | Assume-role trust with external ID |
| `tests/test_iam_policies.py` | Offline proof the policies cannot write (no AWS needed) |
| `tests/test_fixtures_dry_run.py` | Runs every fixture and teardown against `tests/stub_aws/aws`, a stub CLI that never calls AWS; checks manifests and import ID formats |
| `scripts/verify-iam-cannot-write.sh` | Live proof via the IAM policy simulator |
| `fixtures/` | AWS CLI scripts that build fake hand-built infra (F1–F7), each with a manifest of what must be adopted or excluded and the outcome the run must reach |

## Run the checks

```sh
cd solutions/iac-agent
pip install pytest
pytest -q tests                 # offline: policy checks + fixture dry runs (needs jq)

# In the sandbox account only:
export SANDBOX_ACCOUNT_ID=... AWS_REGION=...
STATE_BUCKET=... STATE_KMS_KEY_ARN=... scripts/verify-iam-cannot-write.sh
fixtures/F1-minimal-network/create.sh   # writes fixtures/out/F1-<run>.manifest.json
fixtures/teardown.sh <run-id>           # or no run-id: every fixture resource
```

Fixture scripts refuse to run unless the credentials belong to `SANDBOX_ACCOUNT_ID`.

| Fixture | Builds | Manifest outcome |
| --- | --- | --- |
| F1 minimal network | VPC, 2 subnets, IGW, route table | `pass`: all adopted |
| F2 web stack | Public/private subnets, NAT + EIP, SGs + rules, IAM role + profile, EC2 + EBS, S3 + sub-resources | `pass`: all adopted |
| F3 edge configs | 50 odd tags, SG self/SG-to-SG rules, inline + managed IAM, unconfigured bucket | `pass`: all adopted |
| F4 must exclude | CloudFormation stack, ASG instance, default VPC, service-linked role | `pass`: nothing adopted |
| F5 permission gap | S3 bucket + a scanner policy with S3 reads removed | `blocked` at discover |
| F6 drift mid-run | VPC + subnet; `drift.sh <run>` changes a tag after the scan | `restart` at approve |
| F7 forced replacement | VPC + subnet; manifest says which AZ to mutate in generated code | `hard_stop` at plan |

F2 and F4 run a NAT gateway and/or a t3.micro: tear them down after each run.

## Notes

- `ec2:DescribeInstanceAttribute` returns EC2 user data, which may hold secrets. It is needed for a zero-diff adoption; generated code is secret-scanned and state is encrypted.
- `s3:ListBucket` (needed by Terraform to read a bucket) can list object names, never contents.
