# IaC Agent

Brings hand-built cloud infrastructure under Terraform state with **zero changes to what is running**. Later versions add Pulumi TypeScript and CloudFormation outputs, and IaC-to-IaC migration.

> **The promise:** no resource is created, updated, replaced or deleted. Proof is a `terraform plan` with 0 changes against the live cloud, checked by Terraform, not by the agent.

Status: **V1 code complete, tested offline**. Not yet run against real AWS (needs the sandbox account). Nothing here touches a client account.

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
| `src/iac_agent/core/` | Cloud-neutral core: models, guarded Terraform wrapper, gates, naming, reports |
| `src/iac_agent/core/graph.py`, `cli.py` | The pipeline (LangGraph, two human approvals, repair loop, reports) and its CLI |
| `src/iac_agent/adapters/aws/` | AWS adapter: read-only discovery, ownership classifier, fixed import IDs, opt-in best-effort lister |
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
pip install -e ".[dev]"
pytest -q                       # offline: policies, gates on recorded plans, fixture dry runs (needs jq)

scripts/install-tools.sh        # exact pinned terraform/tflint/gitleaks/checkov into .tools/ (checksummed)
IAC_AGENT_REAL_TOOLS=1 pytest -q tests/test_real_tools.py   # real binaries on sample output, no AWS

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

## Sandbox run (task 6)

```sh
cd solutions/iac-agent && pip install -e ".[dev]" && scripts/install-tools.sh
export SANDBOX_ACCOUNT_ID=... AWS_REGION=...                # sandbox admin credentials in your shell
scripts/sandbox-setup.sh                                    # state bucket + KMS key + agent roles
source fixtures/out/sandbox.env && scripts/verify-iam-cannot-write.sh   # roles provably cannot write

fixtures/F2-web-stack/create.sh                             # prints the run ID and manifest path
python -m iac_agent.harness fixtures/out/F2-<run>.manifest.json --second-run --golden update --cloudtrail-wait 15
fixtures/teardown.sh <run>                                  # always, even after a failure
```

The harness answers the human steps from the manifest (scope = its adopt list), runs `drift.sh` for F6,
mutates the AZ for F7, and checks: outcome and step as expected, state == adopt list, second run gives
the same code, no AWS write calls by the agent's sessions in CloudTrail, golden files (F1, F2).
Result: `fixtures/out/runs/<F>-<run>/HARNESS_RESULT.json`. Review `--golden update` output before committing it.

## Run the agent (sandbox only, once it exists)

```sh
W=runs/f2; R=f2-001; A=$SANDBOX_ACCOUNT_ID; G=$AWS_REGION
iac-agent scan    --run-id $R --workdir $W --account $A --region $G   # stops: $W/scope_signoff.json
iac-agent plan    --run-id $R --workdir $W --region $G [--scope approved.json] [--model-id <bedrock-model>]
                                                                      # stops: $W/approve_plan.json
iac-agent approve --run-id $R --workdir $W --region $G                # re-scan, import, verify, report
```

Without `--model-id` nothing is sent to an LLM: resources that need repair are skipped and reported.
Output in the workdir: generated `.tf` files, `imports.tf`, `ADOPTION_REPORT.md`, `FINDINGS.md`, state backups.

## Notes

- `ec2:DescribeInstanceAttribute` returns EC2 user data, which may hold secrets. It is needed for a zero-diff adoption; generated code is secret-scanned and state is encrypted.
- `s3:ListBucket` (needed by Terraform to read a bucket) can list object names, never contents.
