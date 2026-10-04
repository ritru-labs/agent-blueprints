import json
import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_migration import configured_inventory

from infra_migration.models import Principal
from infra_migration.service.api import create_app
from infra_migration.service.auth import JwtVerifier, actor_id
from infra_migration.service.runtime import Registry, TenantRuntime
from infra_migration.tools import AccessDenied

ISSUER = "https://identity.example.test/"
AUDIENCE = "infra-migration"


@pytest.fixture
def signing():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid="active", use="sig", alg="RS256")
    return private, {"keys": [public]}


def access_token(private, subject="requester", **changes):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "iat": now, "exp": now + 300}
    claims.update(changes)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "active"})


def test_authentication_ignores_claimed_tenant_and_roles(signing):
    private, jwks = signing
    verifier = JwtVerifier(ISSUER, AUDIENCE, jwks)
    token = access_token(private, tenant_id=str(uuid4()), roles=["executor", "admin"])
    assert verifier.verify(token) == actor_id(ISSUER, "requester")


@pytest.mark.parametrize(
    "change",
    ["issuer", "audience", "expired", "future", "lifetime", "kid", "jku", "algorithm", "signature"],
)
def test_jwt_rejects_untrusted_identity_inputs(signing, change):
    private, jwks = signing
    verifier = JwtVerifier(ISSUER, AUDIENCE, jwks)
    now = int(time.time())
    changes = {
        "issuer": {"iss": "https://attacker.example.test/"},
        "audience": {"aud": "other-product"},
        "expired": {"iat": now - 500, "exp": now - 100},
        "future": {"nbf": now + 600},
        "lifetime": {"exp": now + 3601},
    }
    token = access_token(private, **changes.get(change, {}))
    if change in {"kid", "jku"}:
        headers = {"kid": "unknown" if change == "kid" else "active"}
        if change == "jku":
            headers["jku"] = "https://attacker.example.test/keys"
        token = jwt.encode(
            jwt.decode(token, options={"verify_signature": False}),
            private,
            algorithm="RS256",
            headers=headers,
        )
    elif change == "algorithm":
        token = jwt.encode(
            {"sub": "requester"}, "fixture-secret" * 4, algorithm="HS256", headers={"kid": "active"}
        )
    elif change == "signature":
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = access_token(other)
    with pytest.raises(AccessDenied):
        verifier.verify(token)


class ApiStore:
    def __init__(self, tenant):
        self.tenant_id = tenant
        self.members = {
            actor_id(ISSUER, "requester"): ("assessor",),
            actor_id(ISSUER, "reviewer"): ("reviewer",),
        }
        self.payloads = []

    def principal(self, actor):
        if actor not in self.members:
            raise AccessDenied("Not a member")
        return Principal(tenant_id=self.tenant_id, subject=actor, roles=self.members[actor])

    def rate_limit(self, principal):
        pass

    def enqueue(self, principal, payload, key):
        if "assessor" not in principal.roles:
            raise AccessDenied("Assessor required")
        self.payloads.append(payload)
        return {"id": uuid4(), "requester": principal.subject, "status": "QUEUED", "result": None}

    def review(self, principal, run, decision):
        if "reviewer" not in principal.roles:
            raise AccessDenied("Reviewer required")


def api_fixture(signing, tmp_path):
    private, jwks = signing
    inventory = configured_inventory()
    store = ApiStore(inventory.scope.tenant_id)
    registry = Registry(
        {
            store.tenant_id: TenantRuntime(
                store,
                frozenset([inventory.scope.account_id]),
                frozenset(inventory.scope.regions),
                tmp_path / "a",
            )
        }
    )
    client = TestClient(create_app(JwtVerifier(ISSUER, AUDIENCE, jwks), registry))
    body = {
        "inventory": inventory.model_dump(mode="json"),
        "resource_ids": ["vpc-fixture", "subnet-fixture"],
        "idempotency_key": str(uuid4()),
    }
    headers = {"Authorization": "Bearer " + access_token(private)}
    return client, store, body, headers


def test_http_prepare_requires_verified_member_and_scoped_inventory(signing, tmp_path):
    client, store, body, headers = api_fixture(signing, tmp_path)
    url = f"/v1/organizations/{store.tenant_id}/runs"
    assert client.post(url, json=body).status_code == 401
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["execution_enabled"] is False
    assert (
        client.post(f"/v1/organizations/{uuid4()}/runs", json=body, headers=headers).status_code
        == 403
    )
    body["inventory"]["scope"]["account_id"] = "999999999999"
    assert client.post(url, json=body, headers=headers).status_code == 422


def test_token_role_forgery_cannot_grant_review(signing, tmp_path):
    client, store, _, _ = api_fixture(signing, tmp_path)
    token = access_token(signing[0], roles=["reviewer", "executor"])
    response = client.post(
        f"/v1/organizations/{store.tenant_id}/runs/{uuid4()}/review",
        json={"plan_digest": "a" * 64, "acknowledged": True},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 403


def test_http_body_budget_and_validation_redaction(signing, tmp_path):
    client, store, body, headers = api_fixture(signing, tmp_path)
    url = f"/v1/organizations/{store.tenant_id}/runs"
    body["unexpected"] = "customer-secret-do-not-reflect"
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 422
    assert "customer-secret" not in response.text
    assert client.post(url, content=b"x" * 1_000_001, headers=headers).status_code == 413
    assert client.post(url + f"/{uuid4()}/execute", json={}, headers=headers).status_code == 404


def test_registry_denies_shared_account_or_overlapping_artifacts(tmp_path):
    a, b = uuid4(), uuid4()
    first = TenantRuntime(
        ApiStore(a), frozenset(["111111111111"]), frozenset(["us-east-1"]), tmp_path / "a"
    )
    shared = TenantRuntime(ApiStore(b), first.accounts, first.regions, tmp_path / "b")
    with pytest.raises(ValueError, match="multiple organizations"):
        Registry({a: first, b: shared})
    overlap = TenantRuntime(
        ApiStore(b), frozenset(["222222222222"]), first.regions, tmp_path / "a/b"
    )
    with pytest.raises(ValueError, match="disjoint"):
        Registry({a: first, b: overlap})


def test_dashboard_assets_csp_and_session_permissions(signing, tmp_path):
    client, store, _, headers = api_fixture(signing, tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "Migration workspace" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    for asset in ("app.js", "style.css"):
        assert client.get("/dashboard/" + asset).status_code == 200
    assert client.get("/dashboard/private.txt").status_code == 404
    script = client.get("/dashboard/app.js").text
    assert "localStorage" not in script and "sessionStorage" not in script
    assert ".innerHTML" not in script
    route = f"/v1/organizations/{store.tenant_id}/session"
    assert client.get(route).status_code == 401
    assert client.get(route, headers=headers).json()["roles"] == ["assessor"]
