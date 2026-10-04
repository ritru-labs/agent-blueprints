"""PostgreSQL checkpoint isolation using a dedicated restricted role and schema per tenant."""

from contextlib import contextmanager
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row, tuple_row

from .tools import AccessDenied


def tenant_schema(tenant_id: UUID):
    return "infra_t_" + tenant_id.hex


@contextmanager
def postgres_checkpointer(conninfo: str, tenant_id: UUID):
    schema = tenant_schema(tenant_id)
    with psycopg.connect(
        conninfo,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={schema}",
    ) as connection:
        with connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute(
                "SELECT current_schema(), rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user"
            )
            current, superuser, bypass = cursor.fetchone()
            if current != schema or superuser or bypass:
                raise AccessDenied("Checkpoint role must be restricted to its tenant schema")
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname LIKE %s "
                "AND nspname <> %s AND has_schema_privilege(oid,'USAGE'))",
                ("infra_t_%", schema),
            )
            if cursor.fetchone()[0]:
                raise AccessDenied("Checkpoint role has cross-tenant schema access")
        saver = PostgresSaver(connection)
        saver.setup()
        yield saver
