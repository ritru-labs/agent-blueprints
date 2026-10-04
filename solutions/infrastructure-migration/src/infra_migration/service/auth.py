"""Pinned public-key JWT verification; tenant membership is resolved by trusted storage."""

import hashlib
from urllib.parse import urlsplit

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
        self.keys = {}
        for key in public_jwks.get("keys", []):
            if (
                key.get("kty") != "RSA"
                or key.get("alg") != "RS256"
                or key.get("use") != "sig"
                or not isinstance(key.get("kid"), str)
                or not key["kid"]
                or "d" in key
                or key["kid"] in self.keys
            ):
                raise ValueError("Use unique public RS256 signing keys only")
            parsed = jwt.PyJWK.from_dict(key).key
            if not 2048 <= parsed.key_size <= 8192:
                raise ValueError("RSA key strength outside policy")
            self.keys[key["kid"]] = parsed
        if not self.keys:
            raise ValueError("At least one pinned signing key is required")

    def verify(self, token: str) -> str:
        if not isinstance(token, str) or len(token.encode()) > 8192:
            raise AccessDenied("Invalid access token")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or header.get("kid") not in self.keys
                or any(k in header for k in ("jku", "jwk", "x5u", "crit"))
            ):
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
