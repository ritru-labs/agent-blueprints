# Infrastructure migration qualification plan

Production grade is an acceptance status that requires exact-source evidence. This repository currently implements a synthetic assessment foundation. All live execution adapters remain unqualified and disabled.

## Implementation milestones

| Milestone | Work | Exit condition |
| --- | --- | --- |
| M0 foundation | Strict contracts, fixture gateway, LangGraph review, durable local recovery, governance | Local control suite passes; reports cannot claim a live migration |
| M1 read-only assessment | Authenticated control API, PostgreSQL isolation, scoped AWS discovery, source parser, canonical configuration model | Coverage and drift are accurate in qualified AWS fixtures; no cloud writes |
| M2 assisted generation | Official-doc retrieval, typed model proposals, provider schemas, Pulumi generation and isolated verification | Exact candidate artifacts compile and preserve configuration semantics; malicious code tests pass |
| M3 manual adoption | Approval service, credential broker, fenced executor, operation journal, import adapters | Manual VPC and subnet fixtures adopted with preserved IDs and explained zero-change previews |
| M4 CloudFormation transfer | Retention verification, source freeze, exports/dependencies, staged release and import | Ownership is unambiguous and every transfer stage has tested recovery |
| M5 supervised pilot | Customer-scoped migration, operator UI, security review, operational monitoring | Customer acceptance, health checks, recovery rehearsal, complete evidence |
| M6 production qualification | Load, isolation, failover, restore, budget and incident drills | Reviewed release candidate satisfies agreed operational criteria |

Complete each exit condition before enabling the next privilege level. Future providers/frameworks enter through the same milestones per adapter. Do not label an implementation milestone as production acceptance.

## Support matrix

| Resource group | Discovery now | Mapping now | Live adoption |
| --- | --- | --- | --- |
| AWS VPC and subnet | Synthetic metadata | Candidate type tokens | Disabled; first proposed qualification targets |
| AWS security group and S3 bucket | Synthetic metadata | Candidate type tokens | Disabled; configuration subresources need their own qualification |
| IAM, KMS, databases and service-managed resources | None | None | Disabled |
| CloudFormation nested stacks, custom resources and exports | None | None | Disabled |
| Terraform, Azure, GCP, Bicep and other destinations | None | None | Planned extension only |

## Required acceptance campaigns

1. Authorization: wrong tenant, actor role, account, region, destination stack, scope and arbitrary checkpoint IDs fail at every service boundary.
2. Approval: wrong artifact hash, expired authorization, consumed nonce, substituted provider, changed inventory, changed state, self-approval and unauthorized resume fail before side effects.
3. Discovery: untagged resources, service pagination, denied reads, throttling, disabled regions, unsupported types and missing dependency configurations produce explicit gaps.
4. Generation: source/live drift, computed defaults, security rules, encryption, IAM attachments, immutable fields, secret placeholders and unsupported properties remain visible. Never hide unexplained diffs.
5. Runner security: malicious imports, package install hooks, dynamic execution, filesystem traversal, subprocesses, network exfiltration and cloud writes during preview are denied.
6. Adoption: validate import IDs against actual provider identity, retain existing physical IDs, preserve configuration, ensure destination state accuracy and prevent dual ownership.
7. Recovery: inject crashes before intent, after submission, before receipt, during partial batches, during ownership release and after destination import. Reconcile each outcome without blind retries.
8. Concurrency: duplicate requests, simultaneous reviewers, stale locks, worker death, source pipeline races and operator cancellation cannot authorize duplicate or stale writes.
9. Evidence: modified tests, substituted code, false tool results, missing invocation receipts and incorrect source versions cannot satisfy criteria.
10. Operations: backup restore, service restart, dependency updates, retention, alerting, load, API/model budget exhaustion and application health checks behave within agreed limits.

## Evidence package

Record immutable source commit and artifact tree, dependency and provider versions, environment identity, fixture or cloud account scope, configuration baselines, tool invocations and receipts, approval references, operation journal, previews, physical IDs, ownership checks, application health, recovery outcomes, and unresolved gaps. Use execution-backed criterion mappings rather than assigning every test to every requirement. Redact secrets before retention or publication.

The current local suite is foundation evidence only. Live-model, cloud, sandbox, PostgreSQL, pilot and production campaigns must each have their own results. An unavailable platform is untested, not passed.
