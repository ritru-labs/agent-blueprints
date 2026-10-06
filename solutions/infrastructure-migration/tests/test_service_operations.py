import json
import logging
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_service import AUDIENCE, ISSUER, access_token, api_fixture
from test_service import signing as signing

from infra_migration.service.auth import (
    JwtVerifier,
    ManagedJwtVerifier,
    actor_id,
    approved_jwks_url,
    fetch_jwks,
)
from infra_migration.service.launcher import ServiceConfig
from infra_migration.service.operations import Readiness, RequestTelemetry
from infra_migration.tools import AccessDenied


class Source:
    def __init__(self, keys):
        self.keys, self.calls, self.now, self.failure = keys, 0, 0.0, False

    def clock(self):
        return self.now

    def fetch(self, url):
        self.calls += 1
        assert url == ISSUER + "keys"
        if self.failure:
            raise RuntimeError("provider-secret-not-reflected")
        return self.keys

    def verifier(self):
        return ManagedJwtVerifier(
            ISSUER,
            AUDIENCE,
            ISSUER + "keys",
            ttl=60,
            cooldown=10,
            fetcher=self.fetch,
            clock=self.clock,
        )


def test_key_rotation_replaces_revoked_keys_atomically_and_bounds_unknown_kids(signing):
    private, jwks = signing
    source = Source(jwks)
    verifier = source.verifier()
    original = access_token(private)
    assert verifier.verify(original) == actor_id(ISSUER, "requester")
    next_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(next_private.public_key()))
    public.update(kid="rotated", use="sig", alg="RS256")
    rotated = jwt.encode(
        jwt.decode(original, options={"verify_signature": False}),
        next_private,
        algorithm="RS256",
        headers={"kid": "rotated"},
    )
    source.keys = {"keys": [public]}
    source.now = 11
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: verifier.verify(rotated), range(16)))
    assert source.calls == 2
    with pytest.raises(AccessDenied):
        verifier.verify(original)
    for _ in range(20):
        with pytest.raises(AccessDenied):
            verifier.verify(original)
    assert source.calls == 2  # One refresh per cooldown, not per hostile token.


def test_refresh_outage_keeps_unexpired_keys_but_never_extends_expiry(signing):
    private, jwks = signing
    source, token = Source(jwks), access_token(private)
    verifier = source.verifier()
    verifier.verify(token)
    source.failure = True
    source.now = 61
    assert verifier.ready() is False
    for _ in range(10):
        with pytest.raises(AccessDenied, match="unavailable"):
            verifier.verify(token)
    assert source.calls == 2
    source.now = 72
    source.failure = False
    assert verifier.verify(token) == actor_id(ISSUER, "requester")
    assert source.calls == 3


def test_untrusted_header_cannot_cause_key_network_requests(signing):
    private, jwks = signing
    source = Source(jwks)
    verifier = source.verifier()
    claims = jwt.decode(access_token(private), options={"verify_signature": False})
    token = jwt.encode(
        claims,
        private,
        algorithm="RS256",
        headers={
            "kid": "active",
            "jku": "https://attacker.example.test/private",
        },
    )
    with pytest.raises(AccessDenied):
        verifier.verify(token)
    assert source.calls == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://identity.example.test/keys",
        "https://other.example.test/keys",
        ISSUER + "keys?secret=value",
        ISSUER + "keys#fragment",
        "https://user:password@identity.example.test/keys",
        "https://127.0.0.1/keys",
    ],
)
def test_key_endpoint_is_deployment_pinned(endpoint):
    with pytest.raises(ValueError):
        approved_jwks_url(ISSUER, endpoint)


@pytest.mark.parametrize("mutation", ["duplicate", "private", "oversized", "empty", "malformed"])
def test_invalid_key_set_cannot_replace_trusted_keys(signing, mutation):
    private, jwks = signing
    source = Source(jwks)
    verifier = source.verifier()
    token = access_token(private)
    verifier.verify(token)
    invalid = deepcopy(jwks)
    if mutation == "duplicate":
        invalid["keys"] *= 2
    elif mutation == "private":
        invalid["keys"][0]["p"] = "private-material"
    elif mutation == "oversized":
        invalid["keys"] *= 17
    elif mutation == "empty":
        invalid["keys"] = []
    else:
        invalid["keys"] = [42]
    source.keys = invalid
    source.now = 61
    with pytest.raises(AccessDenied):
        verifier.verify(token)
    assert not verifier.ready()
    with pytest.raises(ValueError):
        JwtVerifier(ISSUER, AUDIENCE, invalid)


def test_key_fetch_disallows_redirects_and_bounds_response(monkeypatch, signing):
    real_client = httpx.Client
    seen = []

    def install(response):
        def factory(**kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

            def handler(request):
                seen.append(str(request.url))
                assert "authorization" not in request.headers
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=httpx.ByteStream(response.content),
                )

            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)

    install(httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    with pytest.raises(ValueError):
        fetch_jwks(ISSUER + "keys")
    assert len(seen) == 1
    install(httpx.Response(200, content=b"x" * 65537))
    with pytest.raises(ValueError, match="budget"):
        fetch_jwks(ISSUER + "keys")
    install(httpx.Response(200, json=signing[1]))
    assert fetch_jwks(ISSUER + "keys") == signing[1]


def test_readiness_cached_failure_and_recovery_never_leak_config():
    class Store:
        calls, fail = 0, True

        def ready(self):
            self.calls += 1
            if self.fail:
                raise RuntimeError("database-password")
            return True

    store = Store()
    clock = [0.0]
    verifier = SimpleNamespace(ready=lambda: True)
    registry = SimpleNamespace(tenants={"tenant": SimpleNamespace(store=store)})
    readiness = Readiness(verifier, registry, clock=lambda: clock[0])
    assert not readiness.check()
    assert not readiness.check() and store.calls == 1
    store.fail, clock[0] = False, 6
    assert readiness.check() and store.calls == 2


def test_request_telemetry_logs_templates_only_and_masks_errors(caplog):
    app = FastAPI()
    app.add_middleware(RequestTelemetry)

    @app.get("/resource/{identifier}")
    def broken(identifier: str):
        raise RuntimeError("database-password")

    with caplog.at_level(logging.INFO, logger="infra_migration.requests"):
        response = TestClient(app).get(
            "/resource/customer-secret?access_token=token-secret",
            headers={"Authorization": "Bearer header-secret", "X-Request-ID": "forged"},
        )
    assert response.status_code == 500
    assert "Service unavailable" in response.text
    assert response.headers["x-request-id"] != "forged"
    event = json.loads(caplog.records[-1].message)
    assert event["route"] == "/resource/{identifier}" and event["status"] == 500
    assert event["request_id"] == response.headers["x-request-id"]
    assert all(
        x not in caplog.text + response.text
        for x in (
            "customer-secret",
            "token-secret",
            "header-secret",
            "database-password",
        )
    )


def test_readiness_is_separate_from_process_liveness(signing, tmp_path):
    client, _, _, _ = api_fixture(signing, tmp_path)
    # Fake store has no database probe: fail closed, even while process is alive.
    assert client.get("/health/live").status_code == 200
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json()["status"] == "not_ready"
    assert response.headers["cache-control"] == "no-store"


def test_service_requires_exactly_one_signing_key_source():
    base = json.loads(__import__("pathlib").Path("examples/service-config.json").read_text())
    assert ServiceConfig.model_validate_json(json.dumps(base)).public_jwks_file
    base["public_jwks_url"] = ISSUER + "keys"
    with pytest.raises(ValueError):
        ServiceConfig.model_validate_json(json.dumps(base))
    del base["public_jwks_file"]
    assert ServiceConfig.model_validate_json(json.dumps(base)).public_jwks_url


def test_failed_unknown_key_refresh_preserves_only_unexpired_snapshot(signing):
    private, jwks = signing
    source = Source(jwks)
    verifier = source.verifier()
    token = access_token(private)
    verifier.verify(token)
    source.failure, source.now = True, 11
    unknown = jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        private,
        algorithm="RS256",
        headers={"kid": "unknown"},
    )
    with pytest.raises(AccessDenied):
        verifier.verify(unknown)
    assert verifier.verify(token) == actor_id(ISSUER, "requester")
    assert source.calls == 2
    source.now = 61
    with pytest.raises(AccessDenied):
        verifier.verify(token)
