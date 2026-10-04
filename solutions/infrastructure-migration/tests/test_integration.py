"""Opt-in real local dependencies. These tests never contact a cloud or model endpoint."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_migration import actors, configured_inventory

from infra_migration.generation import generate, write_bundle
from infra_migration.models import ReviewDecision
from infra_migration.persistence import postgres_checkpointer, tenant_schema
from infra_migration.pipeline import MigrationPipeline
from infra_migration.runner import DockerRunner
from infra_migration.tools import AccessDenied


@pytest.mark.skipif(not os.environ.get("INFRA_RUNNER_IMAGE"), reason="Docker runner not configured")
def test_real_sandbox_compiles_pulumi_without_network(tmp_path):
    inventory = configured_inventory()
    bundle = generate(inventory, ("vpc-fixture", "subnet-fixture"))
    directory = tmp_path / "project"
    write_bundle(bundle, directory)
    receipt = DockerRunner(os.environ["INFRA_RUNNER_IMAGE"]).compile(bundle, directory)
    assert receipt["passed"] and receipt["artifact_digest"] == bundle.artifact_digest


@pytest.mark.skipif(
    not os.environ.get("INFRA_TEST_POSTGRES_DSN"), reason="Disposable PostgreSQL not configured"
)
def test_postgres_restart_and_storage_tenant_isolation(tmp_path):
    admin_dsn = os.environ["INFRA_TEST_POSTGRES_DSN"]
    inventory = configured_inventory()
    assessor, reviewer, _ = actors(inventory)
    tenant_b = uuid4()
    schema_a, schema_b = tenant_schema(inventory.scope.tenant_id), tenant_schema(tenant_b)
    role_a, role_b = "a_" + uuid4().hex, "b_" + uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for role, schema in ((role_a, schema_a), (role_b, schema_b)):
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'fixture-password'").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
    dsn_a = make_conninfo(admin_dsn, user=role_a, password="fixture-password")
    dsn_b = make_conninfo(admin_dsn, user=role_b, password="fixture-password")
    try:
        with postgres_checkpointer(dsn_a, inventory.scope.tenant_id) as saver:
            pipeline = MigrationPipeline(
                assessor,
                inventory.scope,
                lambda: inventory,
                saver,
                output_directory=tmp_path / "project",
            )
            result = pipeline.start(("vpc-fixture", "subnet-fixture"))
            plan = result["__interrupt__"][0].value["plan_digest"]
        with postgres_checkpointer(dsn_a, inventory.scope.tenant_id) as saver:
            pipeline = MigrationPipeline(
                assessor,
                inventory.scope,
                lambda: inventory,
                saver,
                output_directory=tmp_path / "project",
            )
            result = pipeline.resume_review(
                reviewer, ReviewDecision(plan_digest=plan, acknowledged=True)
            )
            assert result["status"] == "REVIEWED_EXECUTION_BLOCKED"
        with postgres_checkpointer(dsn_b, tenant_b):
            pass
        with pytest.raises(AccessDenied):
            with postgres_checkpointer(dsn_b, inventory.scope.tenant_id):
                pass
        with psycopg.connect(dsn_b, autocommit=True) as other:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                other.execute(
                    sql.SQL("SELECT * FROM {}.checkpoints").format(sql.Identifier(schema_a))
                )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for role, schema in ((role_a, schema_a), (role_b, schema_b)):
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
