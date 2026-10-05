# Infrastructure Migration Agent operations

These controls are implemented and can be qualified without AWS credentials. They do not establish customer deployment acceptance or permit cloud writes.

## Signing-key sources and rotation

The default configuration uses `public_jwks_file`. It remains suitable for an explicitly pinned key set; revocation requires updating that trusted file and restarting the service.

For managed rotation, remove `public_jwks_file` and set:

```json
{
  "public_jwks_url": "https://YOUR_APPROVED_ISSUER/keys",
  "jwks_ttl_seconds": 300
}
```

Keep the existing explicit `issuer`, `audience`, and tenant provisioning. Exactly one key source is accepted. This example is a configuration fragment, not a standalone service configuration. Verify the real issuer's signing-key endpoint and RS256 access-token contract independently before deployment. Identity-provider discovery is deliberately unavailable. Some providers put keys on a different origin; this version blocks that configuration rather than accepting an arbitrary fetch destination.

Managed refresh sends no customer token, cookies or credentials to the endpoint. TLS verification is enabled; redirects and environment proxy inheritance are disabled. Responses are bounded to 64 KiB, at most 16 unique public RS256 RSA signing keys, per-I/O timeouts and a fetch deadline. Only a deployment-pinned HTTPS issuer-origin URL is allowed. Literal private IPs, localhost, user info, query strings and fragments are rejected. DNS resolution and egress still require trusted deployment controls; this is not an egress firewall.

The cache lasts 60–900 seconds (default 300). New key IDs can trigger refresh at most once per 30 seconds per process. Concurrent callers share an atomic snapshot and one refresh lock. Successful refresh replaces the entire key set, removing revoked keys. A failed or malformed refresh does not replace valid cached keys or extend their original expiry. Once the cache expires, authentication fails closed until a successful refresh. Invalid algorithms, token-supplied URLs and oversized tokens are rejected before network access. Expiry and issuer/audience checks still apply to every token; tenant and role claims remain ignored.

A provider outage therefore affects availability after cache expiry. Key-removal propagation is bounded by the TTL per process; urgent revocation requires the deployment administrator to restart all service processes after changing the trusted source. Subject membership revocation remains an independent database control. No token refresh or browser session is silently extended.

## Process health and readiness

`GET /health/live` checks process liveness only. `GET /health/ready` returns 200/`ready` or 503/`not_ready`, checks signing-key availability, and performs SELECT-only probes of provisioned queue tables using each restricted tenant role. Schema/role isolation is rechecked by the normal database boundary. Empty registries, missing tables, privilege failures, database outages and expired/unavailable identity keys fail closed. No DSN, provider response, tenant name or exception text appears in the response.

Both positive and negative readiness results are cached for five seconds under a lock. The deployment supports at most 32 configured tenants per service process. Database connection and statement timeouts bound individual probes. Readiness may lag a dependency failure/recovery by the cache duration. These checks do not prove worker liveness, model availability, artifact-volume capacity, backup recoverability or successful migration. Ingress should restrict probe frequency and allow enough time for configured dependency checks; use a separate startup grace period. Do not use readiness as a cloud authorization signal.

## Request telemetry

The service launcher enables JSON request events on stderr. Each event contains an application-generated request ID, an application route template, response status, failure flag and duration. `X-Request-ID` on the response permits correlation. Incoming request IDs, literal resource/run/tenant IDs, URLs, query strings, headers, tokens, bodies, generated code and exception text are excluded. Unknown paths are logged as `unmatched` rather than reflected. Unexpected failures before response headers receive a generic error; unexpected application exceptions are not emitted as payload-bearing tracebacks by this middleware.

Collect these events in the deployment's log pipeline. Configure alerts for sustained 401/403/429/5xx rates, readiness failures and request latency. Request events are operational telemetry, not a tamper-proof governance audit trail, migration evidence or worker heartbeat. Secure retention, access policy and immutable event export remain deployment requirements. Access logs remain disabled; the ingress must also redact sensitive headers and query strings.

## Remaining acceptance

Before a customer pilot, independently verify provider key rollover/removal and outage recovery, TLS/egress/secret custody, least-privilege provisioning, managed worker restart, encrypted artifacts, backup/restore and recovery, monitoring/alerts, SSO, and live cloud/model qualification. Passing fixture/CI checks does not replace these gates.

JWT signature validation uses [PyJWT](https://pyjwt.readthedocs.io/en/latest/api.html); the API uses [FastAPI middleware](https://fastapi.tiangolo.com/tutorial/middleware/).
