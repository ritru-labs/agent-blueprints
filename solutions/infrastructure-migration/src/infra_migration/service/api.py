"""Authenticated organization-scoped preparation API. There is no cloud execution route."""

import asyncio
import io
import json
import zipfile
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ConfigDict, Field, field_validator

from ..generation import ProjectBundle, verify_bundle_directory
from ..models import Contract, Inventory, ReviewDecision
from ..tools import AccessDenied
from .runtime import Registry


class PrepareRequest(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)
    inventory: Inventory
    resource_ids: tuple[str, ...] = Field(min_length=1, max_length=1000)
    idempotency_key: UUID

    @field_validator("inventory", mode="before")
    @classmethod
    def parse_inventory(cls, value):
        if isinstance(value, dict):
            return Inventory.model_validate_json(json.dumps(value))
        return value


class BodyLimit:
    """Bound total streamed request bytes before JSON parsing, including chunked uploads."""

    def __init__(self, app, max_bytes=1_000_000):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        total, chunks = 0, []
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    total += len(message.get("body", b""))
                    if total > self.max_bytes or len(chunks) >= 8192:
                        return await JSONResponse({"detail": "Request exceeds byte budget"}, 413)(
                            scope, receive, send
                        )
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body deadline exceeded"}, 408)(
                scope, receive, send
            )
        delivered = False

        async def limited_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, limited_receive, send)


def public_job(row):
    return {
        "run_id": str(row["id"]),
        "status": row["status"],
        "result": row["result"],
        "execution_enabled": False,
    }


def create_app(verifier, registry: Registry):
    app = FastAPI(
        title="Infrastructure migration preparation service", docs_url=None, redoc_url=None
    )
    app.add_middleware(BodyLimit)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(AccessDenied)
    async def denied(request, exc):
        return JSONResponse({"detail": "Access denied or operation unavailable"}, 403)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "Request does not match the contract"}, 422)

    def authorized(
        tenant: UUID,
        token: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if token is None:
            raise HTTPException(
                401, "Valid bearer access token required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            actor = verifier.verify(token.credentials)
        except AccessDenied:
            raise HTTPException(
                401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"}
            ) from None
        runtime = registry.get(tenant)
        principal = runtime.store.principal(actor)
        runtime.store.rate_limit(principal)
        return runtime, principal

    @app.get("/health/live")
    def health():
        return {"status": "alive", "cloud_execution": "disabled"}

    @app.post("/v1/organizations/{tenant}/runs", status_code=202)
    def prepare(body: PrepareRequest, context: Annotated[tuple, Depends(authorized)]):
        runtime, principal = context
        scope = body.inventory.scope
        if (
            scope.tenant_id != principal.tenant_id
            or scope.account_id not in runtime.accounts
            or not set(scope.regions) <= runtime.regions
            or len(set(body.resource_ids)) != len(body.resource_ids)
        ):
            raise AccessDenied("Inventory or selection is outside provisioned scope")
        payload = {
            "inventory": body.inventory.model_dump(mode="json"),
            "resource_ids": list(body.resource_ids),
        }
        return public_job(runtime.store.enqueue(principal, payload, body.idempotency_key))

    @app.get("/v1/organizations/{tenant}/runs/{run}")
    def get_run(run: UUID, context: Annotated[tuple, Depends(authorized)]):
        runtime, principal = context
        return public_job(runtime.store.get(principal, run))

    @app.post("/v1/organizations/{tenant}/runs/{run}/review", status_code=202)
    def review(run: UUID, body: ReviewDecision, context: Annotated[tuple, Depends(authorized)]):
        runtime, principal = context
        runtime.store.review(principal, run, body)
        return {"status": "REVIEW_QUEUED", "execution_enabled": False}

    @app.post("/v1/organizations/{tenant}/runs/{run}/cancel")
    def cancel(run: UUID, context: Annotated[tuple, Depends(authorized)]):
        runtime, principal = context
        runtime.store.cancel(principal, run)
        return {"status": "CANCELLED", "execution_enabled": False}

    @app.get("/v1/organizations/{tenant}/runs/{run}/artifacts")
    def artifacts(tenant: UUID, run: UUID, context: Annotated[tuple, Depends(authorized)]):
        runtime, principal = context
        job = runtime.store.get(principal, run)
        if job["status"] not in {"AWAITING_REVIEW", "REVIEWED_EXECUTION_BLOCKED", "REJECTED"}:
            raise AccessDenied("Artifacts are unavailable at this stage")
        directory = registry.directory(tenant, run)
        manifest = directory / "bundle.json"
        if directory.is_symlink() or manifest.is_symlink():
            raise AccessDenied("Artifact links are not permitted")
        try:
            if manifest.stat().st_size > 32_000_000:
                raise AccessDenied("Artifact manifest exceeds budget")
            bundle = ProjectBundle.model_validate_json(manifest.read_text())
        except (OSError, ValueError):
            raise AccessDenied("Artifact manifest is invalid or unavailable") from None
        if bundle.artifact_digest != job["result"]["artifact_digest"]:
            raise AccessDenied("Artifact digest changed")
        verify_bundle_directory(bundle, directory)
        if sum(len(v.encode()) for v in bundle.files.values()) > 16_000_000:
            raise AccessDenied("Artifact export exceeds budget")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in bundle.files.items():
                archive.writestr(name, content)
            archive.writestr("bundle.json", bundle.model_dump_json(indent=2))
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="migration-{run}.zip"',
                "Cache-Control": "no-store",
            },
        )

    @app.middleware("http")
    async def response_controls(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app
