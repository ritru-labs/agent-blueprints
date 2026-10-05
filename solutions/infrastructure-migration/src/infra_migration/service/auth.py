"""Pinned public-key JWT verification; tenant membership is resolved by trusted storage."""

import hashlib
import ipaddress
import json
import threading
import time
from urllib.parse import urlsplit

import httpx
import jwt

from ..tools import AccessDenied


def actor_id(issuer: str, subject: str) -> str:
    return hashlib.sha256((issuer + "\0" + subject).encode()).hexdigest()


class JwtVerifier:
    def __init__(self, issuer: str, audience: str, public_jwks: dict, *, max_lifetime=900):
        url = urlsplit(issuer)
        if url.scheme != "https" or not url.hostname or url.username or url.fragment:
            raise ValueError("Configure an explicit HTTPS identity issuer")
        if not audience or not 60 <= max_lifetime <= 3600:
            raise ValueError("Invalid audience or token lifetime")
        self.issuer, self.audience, self.max_lifetime = issuer, audience, max_lifetime
        if not isinstance(public_jwks, dict):
            raise ValueError("Invalid public signing-key set")
        key_set = public_jwks.get("keys")
        if not isinstance(key_set, list) or not 1 <= len(key_set) <= 16:
            raise ValueError("Signing-key set exceeds policy")
        self.keys = {}
        for key in key_set:
            if (
                not isinstance(key, dict)
                or key.get("kty") != "RSA"
                or key.get("alg") != "RS256"
                or key.get("use") != "sig"
                or not isinstance(key.get("kid"), str)
                or not key["kid"]
                or any(k in key for k in ("d", "p", "q", "dp", "dq", "qi", "oth", "k"))
                or key["kid"] in self.keys
            ):
                raise ValueError("Use unique public RS256 signing keys only")
            parsed = jwt.PyJWK.from_dict(key).key
            if not 2048 <= parsed.key_size <= 8192:
                raise ValueError("RSA key strength outside policy")
            self.keys[key["kid"]] = parsed
        if not self.keys:
            raise ValueError("At least one pinned signing key is required")

    def ready(self) -> bool:
        return True

    def verify(self, token: str) -> str:
        header = access_header(token)
        try:
            if header["kid"] not in self.keys:
                raise AccessDenied("Invalid access token")
            claims = jwt.decode(
                token,
                self.keys[header["kid"]],
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "sub", "iat", "exp"]},
                leeway=0,
            )
            if (
                not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 512
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= self.max_lifetime
            ):
                raise AccessDenied("Invalid access token")
            # Token tenant/role claims are deliberately ignored.
            return actor_id(self.issuer, claims["sub"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise AccessDenied("Invalid access token") from None


def access_header(token: str) -> dict:
    if not isinstance(token, str) or len(token.encode()) > 8192:
        raise AccessDenied("Invalid access token")
    try:
        header = jwt.get_unverified_header(token)
        if (
            header.get("alg") != "RS256"
            or not isinstance(header.get("kid"), str)
            or not 1 <= len(header["kid"]) <= 256
            or any(k in header for k in ("jku", "jwk", "x5u", "crit"))
        ):
            raise AccessDenied("Invalid access token")
        return header
    except (jwt.PyJWTError, TypeError, ValueError):
        raise AccessDenied("Invalid access token") from None


def approved_jwks_url(issuer: str, endpoint: str) -> str:
    """Only a deployment-pinned HTTPS endpoint on the issuer's origin is permitted."""
    origin, url = urlsplit(issuer), urlsplit(endpoint)
    if (
        origin.scheme != "https"
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or url.scheme != "https"
        or not url.hostname
        or (url.hostname, url.port) != (origin.hostname, origin.port)
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.hostname == "localhost"
        or url.hostname.endswith((".localhost", ".local"))
    ):
        raise ValueError("Pin an HTTPS signing-key endpoint on the identity issuer origin")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Signing-key endpoint must not be a private address")
    return endpoint


def fetch_jwks(endpoint: str) -> dict:
    """TLS-verified bounded fetch with no redirects, proxy inheritance or customer headers."""
    deadline = time.monotonic() + 5
    with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "GET", endpoint, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("Signing-key endpoint unavailable")
            content = bytearray()
            for chunk in response.iter_raw():
                content.extend(chunk)
                if len(content) > 65536 or time.monotonic() > deadline:
                    raise ValueError("Signing-key fetch budget exceeded")
    return json.loads(content)


class ManagedJwtVerifier:
    """Atomic key replacement, bounded refresh and fail-closed expiry; never trust token URLs."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        endpoint: str,
        *,
        ttl=300,
        cooldown=30,
        fetcher=fetch_jwks,
        clock=time.monotonic,
    ):
        if not audience or not 60 <= ttl <= 900 or not 5 <= cooldown <= ttl:
            raise ValueError("Invalid signing-key refresh bounds")
        self.endpoint = approved_jwks_url(issuer, endpoint)
        self.issuer, self.audience = issuer, audience
        self.ttl, self.cooldown = ttl, cooldown
        self.fetcher, self.clock = fetcher, clock
        self._lock = threading.Lock()
        self._verifier = None
        self._expires = 0.0
        self._next_refresh = 0.0

    def _snapshot(self, kid=None):
        with self._lock:
            now = self.clock()
            valid = self._verifier is not None and now < self._expires
            missing = kid is not None and (not valid or kid not in self._verifier.keys)
            if (not valid or missing) and now >= self._next_refresh:
                self._next_refresh = now + self.cooldown
                try:
                    candidate = JwtVerifier(self.issuer, self.audience, self.fetcher(self.endpoint))
                except Exception:
                    # Preserve still-valid keys on refresh failure; never extend their expiry.
                    candidate = None
                if candidate is not None:
                    self._verifier = candidate
                    self._expires = self.clock() + self.ttl
                valid = self._verifier is not None and self.clock() < self._expires
            if not valid or (kid is not None and kid not in self._verifier.keys):
                raise AccessDenied("Identity signing keys unavailable")
            return self._verifier

    def verify(self, token: str) -> str:
        header = access_header(token)  # Reject substitution/URLs before any network call.
        return self._snapshot(header["kid"]).verify(token)

    def ready(self) -> bool:
        try:
            self._snapshot()
            return True
        except AccessDenied:
            return False
