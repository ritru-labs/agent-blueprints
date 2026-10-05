"""Explicit service/bootstrap/worker entrypoints with trusted deployment configuration."""

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from uuid import UUID

import uvicorn
from pydantic import ConfigDict, Field, model_validator

from ..models import Contract
from ..reasoning import ModelReviewer, fetch_document
from ..runner import DockerRunner
from .api import create_app
from .auth import JwtVerifier, ManagedJwtVerifier, actor_id
from .runtime import PreparationWorker, Registry, TenantRuntime
from .storage import TenantStore


class MemberConfig(Contract):
    subject: str = Field(min_length=1, max_length=512)
    roles: tuple[str, ...]
    active: bool = True


class TenantConfig(Contract):
    tenant_id: UUID
    dsn_environment: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,100}$")
    accounts: tuple[str, ...]
    regions: tuple[str, ...]
    artifact_directory: str
    members: tuple[MemberConfig, ...] = ()


class ServiceConfig(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    issuer: str
    audience: str
    public_jwks_file: str | None = None
    public_jwks_url: str | None = None
    jwks_ttl_seconds: int = Field(default=300, ge=60, le=900)
    tenants: tuple[TenantConfig, ...]
    compiler_image: str | None = None
    model_name: str | None = None
    model_base_url: str | None = None
    official_documents: bool = False

    @model_validator(mode="after")
    def key_source(self):
        if bool(self.public_jwks_file) == bool(self.public_jwks_url):
            raise ValueError("Configure exactly one trusted signing-key source")
        if not 1 <= len(self.tenants) <= 32:
            raise ValueError("Provision between one and 32 tenants per service")
        return self


def load(path: Path):
    config = ServiceConfig.model_validate_json(path.read_text())
    if not config.tenants or len({t.tenant_id for t in config.tenants}) != len(config.tenants):
        raise ValueError("Provision unique tenant identities")
    if config.model_base_url and not config.model_name:
        raise ValueError("A model endpoint needs an explicit model")
    if config.public_jwks_url:
        verifier = ManagedJwtVerifier(
            config.issuer, config.audience, config.public_jwks_url, ttl=config.jwks_ttl_seconds
        )
    else:
        jwks = (path.parent / config.public_jwks_file).resolve()
        verifier = JwtVerifier(config.issuer, config.audience, json.loads(jwks.read_text()))
    tenants = {}
    for tenant in config.tenants:
        if (
            not tenant.accounts
            or any(not re.fullmatch(r"[0-9]{12}", a) for a in tenant.accounts)
            or not tenant.regions
            or len(set(tenant.accounts)) != len(tenant.accounts)
            or len(set(tenant.regions)) != len(tenant.regions)
        ):
            raise ValueError("Provision bounded account/region allowlists")
        dsn = os.environ.get(tenant.dsn_environment)
        if not dsn:
            raise ValueError("Required tenant database environment is not configured")
        directory = Path(tenant.artifact_directory)
        if not directory.is_absolute():
            directory = path.parent / directory
        tenants[tenant.tenant_id] = TenantRuntime(
            TenantStore(dsn, tenant.tenant_id),
            frozenset(tenant.accounts),
            frozenset(tenant.regions),
            directory,
        )
    return config, verifier, Registry(tenants)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="Explicitly initialize tenant tables and memberships")
    serve = commands.add_parser("serve", help="Run authenticated preparation HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    worker = commands.add_parser("worker", help="Run preparation-only worker")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    config, verifier, registry = load(args.config)
    if args.command == "bootstrap":
        for tenant in config.tenants:
            store = registry.get(tenant.tenant_id).store
            store.setup()
            for member in tenant.members:
                store.provision_member(
                    actor_id(config.issuer, member.subject), member.roles, active=member.active
                )
        print(json.dumps({"tenants_initialized": len(config.tenants), "cloud_writes": 0}))
    elif args.command == "serve":
        logger = logging.getLogger("infra_migration.requests")
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())
        logger.propagate = False
        uvicorn.run(
            create_app(verifier, registry),
            host=args.host,
            port=args.port,
            access_log=False,
            limit_concurrency=64,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=30,
        )
    else:
        runner = DockerRunner(config.compiler_image) if config.compiler_image else None
        documents = (
            tuple(fetch_document(key) for key in ("vpc", "subnet", "adoption"))
            if config.official_documents
            else ()
        )
        model_factory = (
            (lambda: ModelReviewer.openai_compatible(config.model_name, config.model_base_url))
            if config.model_name
            else None
        )
        prepare = PreparationWorker(
            registry, runner=runner, model_factory=model_factory, documents=documents
        )
        while True:
            for tenant in registry.tenants:
                prepare.once(tenant)
            if args.once:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
