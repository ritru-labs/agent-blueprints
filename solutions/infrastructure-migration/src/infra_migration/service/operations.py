"""Bounded readiness and payload-free request telemetry; these are not an audit ledger."""

import json
import logging
import threading
import time
from uuid import uuid4

LOGGER = logging.getLogger("infra_migration.requests")


class RequestTelemetry:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id, started, status = uuid4().hex, time.monotonic(), 500
        response_started, completed, failed = False, False, False

        async def tracked(message):
            nonlocal status, response_started, completed
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode())
                ]
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            # No traceback, request URL, exception text or customer data in this logger.
            failed = True
            if not response_started:
                status = 500
                await tracked(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff"),
                            (b"content-security-policy", b"default-src 'none'"),
                            (b"referrer-policy", b"no-referrer"),
                        ],
                    }
                )
                await tracked(
                    {"type": "http.response.body", "body": b'{"detail":"Service unavailable"}'}
                )
            elif not completed:
                await tracked({"type": "http.response.body", "body": b""})
        finally:
            route = scope.get("route")
            template = getattr(route, "path", "unmatched")
            # The template is application-owned; never log path/query or incoming headers.
            LOGGER.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "route": template,
                        "status": status,
                        "failed": failed,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                )
            )


class Readiness:
    def __init__(self, verifier, registry, *, clock=time.monotonic):
        self.verifier, self.registry, self.clock = verifier, registry, clock
        self._lock, self._expires, self._ready = threading.Lock(), 0.0, False

    def check(self):
        # Cache both success and failure so unauthenticated probes cannot flood databases.
        with self._lock:
            if self.clock() < self._expires:
                return self._ready
            try:
                self._ready = bool(self.registry.tenants) and self.verifier.ready()
                if self._ready:
                    for runtime in self.registry.tenants.values():
                        if not runtime.store.ready():
                            self._ready = False
                            break
            except Exception:
                self._ready = False
            self._expires = self.clock() + 5
            return self._ready
