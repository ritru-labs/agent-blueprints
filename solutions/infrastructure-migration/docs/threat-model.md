# Infrastructure migration threat model

Protect cloud resources, tenant data, credentials, configuration integrity, management state, approvals, generated code, and acceptance evidence. The model, customer source files, retrieved documents, generated dependencies, and tool output are untrusted. Authenticated control services, policy enforcement, credential issuance, and evidence verification form the trusted computing base.

| Threat | Required control | Qualification evidence |
| --- | --- | --- |
| Cross-tenant reads or execution | Authenticated tenant binding at API, gateway, checkpoint and object-store access; database isolation | Attempts across start, read, resume, artifacts and execution are denied |
| Prompt injection in tags, code or docs | Data-only ingestion, allowlisted tools, deterministic authority | Malicious fixtures cannot change scope or call privileged tools |
| Cloud credential theft | Brokered short-lived credentials, runner isolation, redaction, egress restrictions | Canary credentials do not escape prompts, logs, artifacts or network |
| Malicious Pulumi preview code | Disposable sandbox, read credentials, restricted network/filesystem/processes | Attempted shell escape, credential exfiltration and forbidden API calls fail |
| Forged or stale approval | Authenticated approver, immutable bindings, expiry and atomic consumption | Modified code, plan, provider, inventory or state invalidates authorization |
| Destructive ownership handoff | Verified retention, frozen source automation, staged batches, dependency checks | Existing IDs retained and source cannot update released resources |
| Duplicate writes after crash | Operation journal, locks, reconciliation, idempotency where supported | Crash campaigns around each external effect do not replay uncertain writes |
| Faked discovery completeness | Service coverage, pagination and permission receipts | Denied access and truncated results remain visible and block affected scope |
| Faked migration success | Trusted immutable verifier receipts and health checks | Missing or substituted receipts cannot satisfy acceptance |
| Budget exhaustion or retry loops | Hard per-run budgets, deadlines, bounded repair | Throttling and endless repair stop predictably with partial evidence |
| State or checkpoint compromise | Restricted store APIs, encryption, isolation, integrity checks, backups | Restore drills and tampering tests detect mismatched authority |

## Current limits

Control tests exercise scope, approval consumption and expiry, resource locking, unknown outcomes, artifact integrity, bounded parsing, model minimization, and drift. Separate real Docker compilation and PostgreSQL role/schema restart tests exercise their exact local dependency contracts. These do not establish comprehensive sandbox containment, hosted enterprise authentication, deployed credential safety, live cloud recovery, or provider parity. SQLite is controlled by the local operator. Production deployment requires independently qualified service boundaries.

## Review before a pilot

Review data residency, model endpoint retention, incident access, operator roles, audit retention, credential issuance, customer deployment requirements, and recovery responsibilities with the customer. Capture configuration choices and residual risk. Independently review gateway and execution code before enabling live operations; model self-review is insufficient evidence.
