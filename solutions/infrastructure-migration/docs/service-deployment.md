# Authenticated preparation service

The service provides verified JWT authentication, database-backed organization roles, durable PostgreSQL preparation/review jobs, protected artifact export, and a PostgreSQL approval/operation ledger. It does not expose cloud execution. AWS/model credentials are unnecessary for its fixture campaign.

## Trust and provisioning

Use access tokens from one explicitly configured HTTPS identity issuer and audience. Provision public RS256 signing keys in a trusted JWKS file. Token signatures, issuer, audience, expiry, future validity, and a maximum 15-minute lifetime are checked. Unknown keys, algorithm substitution, and token-directed key URLs are rejected. Token tenant/role claims are ignored. Provision membership using the issuer's immutable subject identifier, not an unverified email or display name. Rotate/revoke pinned keys by updating trusted configuration and restarting the service; automatic identity-provider discovery/rotation and browser SSO login are not implemented.

An administrator creates a restricted PostgreSQL login and schema named `infra_t_<tenant UUID without hyphens>` for each organization. The role must not be superuser, bypass RLS, or have usage of another tenant schema. Configure its DSN through a named environment variable; do not embed passwords in configuration, arguments, evidence, or logs. Dedicated tenant roles own their own tables; database administrators remain trusted. Separate artifact directories must be disjoint. Each AWS account belongs to one provisioned organization. This prevents resource locks being split across tenants with overlapping account ownership.

Copy `examples/service-config.json` into a trusted deployment directory and replace account/region/subject/key configuration. Provide the public JWKS file separately. Run:

```sh
infra-migration-service --config /trusted/service-config.json bootstrap
infra-migration-service --config /trusted/service-config.json serve
infra-migration-service --config /trusted/service-config.json worker --once
```

Bootstrap creates tenant tables and applies explicitly configured memberships. It cannot create database roles, schemas, cloud permissions, or identity accounts. To revoke a member, configure `active: false` and rerun bootstrap; removing an entry alone is not revocation. There is no public membership administration route. Run `worker` without `--once` only as an explicitly deployed managed service. Deployment configuration can optionally select an immutable compiler image, approved model endpoint/model, and official-document retrieval; all are absent by default.

## API workflow

All organization routes require `Authorization: Bearer <access token>`. Do not put tokens in URLs. Access logs are disabled; TLS termination, request-header redaction and ingress controls are deployment responsibilities.

| Route | Behavior |
| --- | --- |
| `POST /v1/organizations/{tenant}/runs` | Assessor submits an inventory snapshot, resource IDs and UUID idempotency key; returns a queued run |
| `GET /v1/organizations/{tenant}/runs/{run}` | Active organization member reads status, blockers, checks and review digest |
| `GET /v1/organizations/{tenant}/runs/{run}/artifacts` | Member downloads the exact verified project ZIP after preparation |
| `POST /v1/organizations/{tenant}/runs/{run}/review` | Distinct reviewer submits `plan_digest` and `acknowledged`; queues durable review |
| `POST /v1/organizations/{tenant}/runs/{run}/cancel` | Requester cancels queued or awaiting-review preparation |
| `GET /health/live` | Reports process liveness and disabled cloud execution; does not prove database readiness |

The worker assigns run identity, marks all uploaded inventory as an unverified operator snapshot, and adds a coverage blocker. An uploaded claim of AWS discovery or completeness cannot authorize adoption. The LangGraph prepares and validates the package, persists an interrupt, and queues review through the same checkpoint after restart. A completed review has `REVIEWED_EXECUTION_BLOCKED`, never migration success. There is no apply/import/release execution route or tenant-controlled adapter admission.

Requests are limited to 1 MB including streamed bodies, active organization work to 100 queued/running jobs, and authenticated requests to 120 per actor per minute. Idempotency keys bind an exact actor/request digest. Worker claims use row locks, lease UUIDs, expiry and three attempts per preparation/review phase. Advisory run locks prevent concurrent checkpoint processing, and stale leases cannot publish results. Expired preparation may be replayed because this graph performs no cloud writes. Cancellation of running work and forced worker takeover are deliberately unavailable. Bound runner/model budgets separately; uncertain external operations are never replayed through this queue.

## Execution storage

`PostgresLedger` is compatible with the existing executor and supplies atomic approval consumption, resource locks across runs, durable model budgets, expiry recheck at submission, tenant-bound operation reads, bound verifier receipts, and unknown-outcome reconciliation. Membership is revalidated at service/ledger boundaries. These APIs belong to trusted execution services; the preparation HTTP API does not expose raw execution bindings or approval minting. Adapter admission remains an empty allowlist by default.

## Deployment acceptance still required

Configure TLS, approved identity-provider access tokens and key rotation, database transport encryption and backups, artifact-volume encryption and retention, ingress timeouts/rate limits, least-privilege runtime identities, managed worker restart, monitoring, alerting and restore tests. PostgreSQL and filesystem administrators can alter records; this implementation does not provide tamper-proof audit storage. Connection pooling, high-availability deployment, live identity-provider interoperability, browser dashboard/login, real cloud orchestration, and operational acceptance remain pending. Do not label a passing local/CI service campaign as customer production acceptance.

Authentication implementation follows the [PyJWT validation API](https://pyjwt.readthedocs.io/en/latest/api.html); HTTP security and request testing use [FastAPI security](https://fastapi.tiangolo.com/reference/security/) and [TestClient](https://fastapi.tiangolo.com/reference/testclient/).
