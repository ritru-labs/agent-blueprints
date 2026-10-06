# Handoff: IaC agent

Live state of the work. `CLAUDE.md` (rules, scope, pipeline) and `docs/SCOPE_AND_ROADMAP.md` (full scope) stay the source of truth; this file says where the work stands and what to do next. Update it at the end of every task.

Updated 2026-10-06. Branch `feat/iac-agent-v0`. Supraj is the user you work with (product owner and approver).

## Where it stands

- **V0 and V1 are built and tested offline; nothing has run against AWS yet.** CLAUDE.md tasks 1–5 are done. Task 6 (sandbox run) is the only open task and it closes both V0 and V1-fixtures.
- 182 tests run by default, 188 with `IAC_AGENT_REAL_TOOLS=1` (real terraform 1.16.5 + tflint/gitleaks/checkov, no cloud). Ruff and shellcheck clean.
- CI: draft PR #4 (`feat/iac-agent-v0` → `main`) exists **only** to run `.github/workflows/iac-agent.yml`. It passes. Keep it a draft; merging is Supraj's decision after the pilot.
- V2–V5 and other clouds are not started, by design: each version starts only after the previous one passes its gate.

## Next step: task 6, run F1–F7 in the sandbox

Blocked until Supraj gives you (1) the sandbox account ID and region and (2) a way to authenticate: an AWS profile name, or temporary credentials exported in the shell. The machine has only a `[default]` profile of unknown account: never call AWS with it, and never call AWS before `SANDBOX_ACCOUNT_ID` is set.

Work in `solutions/iac-agent`, one step at a time, stopping at the first failure:

1. **Identity.** `aws sts get-caller-identity` (with the given profile) returns exactly the sandbox account. Done when the account matches.
2. **Setup.** `export SANDBOX_ACCOUNT_ID=… AWS_REGION=…` then `scripts/sandbox-setup.sh`. Done when `fixtures/out/sandbox.env` holds all seven exports.
3. **IAM proof (closes V0's IAM half).** `source fixtures/out/sandbox.env && scripts/verify-iam-cannot-write.sh`. Done when it exits 0 and every line is `ok`.
4. **F1.** `fixtures/F1-minimal-network/create.sh` (prints the run ID), then
   `.venv/bin/python -m iac_agent.harness fixtures/out/F1-<run>.manifest.json --second-run --golden update --cloudtrail-wait 15`,
   then always `fixtures/teardown.sh <run>`. Done when `fixtures/out/runs/F1-<run>/HARNESS_RESULT.json` says `"ok": true`, teardown prints `Teardown done.`, and `aws ec2 describe-vpcs --filters Name=tag:iac-agent-run,Values=<run>` returns nothing.
5. **F2–F7**, same loop, in order. F2 and F4 run a NAT gateway / t3.micro: tear down right after each. The harness handles each fixture's special case (F5 session policy, F6 `drift.sh`, F7 AZ mutation) from the manifest.
6. **Report each fixture to Supraj**: ok or not, what was skipped and why, CloudTrail result. Ask him to review `tests/golden/F1` and `F2` before you commit them.

Task 6 is done when all seven `HARNESS_RESULT.json` files are `ok`, every teardown is clean, and Supraj has approved the golden files.

## When a sandbox run fails (expect some on the first pass)

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `AccessDenied` in a plan or discovery gate | Scanner/importer lacks a read the provider needs | Propose the exact action; **ask Supraj before changing IAM**. Then edit `iam/*.json`, keep `tests/test_iam_policies.py` green, re-run `scripts/sandbox-setup.sh` and the verify script. |
| Plan gate `REPAIR` that never converges, resource skipped | Generated config differs from live (provider defaults, `tags_all`) | Note type + attribute. Without `--model-id` nothing is repaired; that is expected. Fix deterministically in code where possible. |
| Classification mismatch in `HARNESS_RESULT.json` | A real AWS default or ownership signal differs from our assumption | Fix in `adapters/aws/ownership.py` with a recorded-response test; it is a product rule, so tell Supraj. |
| `aws_key_pair` skipped | AWS does not return the public key on import | Acceptable (skipped with reason); report it. |
| CloudTrail check finds nothing at all | `Username` lookup may not match assumed-role session names | Verify with one known event before trusting the "no writes" result. |
| Harness or teardown crashes mid-run | Partial infra left behind | Run `fixtures/teardown.sh <run>` (or with no run ID: everything fixture-tagged) before anything else. |

## Decisions waiting for Supraj

The code already implements the recommended default for each; confirm or change:

1. Default VPC: its AWS-made parts are excluded; hand-built subnets/SGs/instances **inside** it are adopted.
2. AWS-applied defaults (allow-all egress on a new SG; SSE-S3, Block Public Access, BucketOwnerEnforced on a new bucket) are **adopted**, since the API returns them like hand-set values.
3. Root volumes and primary ENIs are excluded (managed through `aws_instance`).
4. Stack: Terraform 1.16.x (OpenTofu in V2), Claude on Bedrock in the client's region, flat files per service, repo private until the pilot.
5. LLM in the first sandbox runs: recommended no LLM first, then Bedrock (needs a model ID and a profile allowed to call it; the scanner role is denied Bedrock).

## Working agreements and gotchas from this session

- Supraj wants continuous progress: after each task test, commit, push, give a short report, and carry on. Stop for: sandbox access, a conflict with CLAUDE.md's hard rules, any IAM change, or a product decision.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push to `main`.
- Python: use `.venv/bin/python` (3.12, created with `uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]'`). Pinned tools: `scripts/install-tools.sh` → `.tools/bin` (the CLI and harness put it on PATH).
- Shell scripts must stay bash 3.2 compatible (macOS default). macOS has no `timeout`; use `perl -e 'alarm N; exec @ARGV' cmd`.
- Terraform must never inherit `AWS_PROFILE`: the AWS SDK prefers a named profile over the importer role's env credentials. `Terraform(env={..., "AWS_PROFILE": None})` removes it; keep that in any new caller.
- The repo lives in `~/Documents` (iCloud). "Optimize Mac Storage" is now off. If git hangs or reports `Operation canceled`, run `find . -flags +dataless`; restore stock `.git` template files from `$(git --exec-path)/../../share/git-core/templates`, and rebuild `.venv`/`.tools` rather than waiting for downloads.
- Workflow YAML: quote any step name containing `: ` (an unparseable workflow silently ran no CI for several commits; `tests/test_ci_workflow.py` now guards it).

## Check the baseline before you start

```sh
cd solutions/iac-agent
.venv/bin/python -m pytest -q                                   # 182 passed, 6 skipped
IAC_AGENT_REAL_TOOLS=1 .venv/bin/python -m pytest -q            # 188 passed (downloads providers)
.venv/bin/ruff check src tests && shellcheck -x -S style scripts/*.sh fixtures/*.sh fixtures/*/*.sh tests/stub_aws/aws
```
