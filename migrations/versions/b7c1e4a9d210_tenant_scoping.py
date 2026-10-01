"""tenant scoping for patterns, reliability, predictions and idempotency keys.

The initial-schema migration is generated from current ORM metadata. On a fresh
database that metadata may already contain some of the tenant-scoped indexes or
constraints introduced here. This migration therefore treats those objects as
idempotent and only creates what is actually missing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1e4a9d210"
down_revision: str | None = "0f52a83618c9"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    inspector = _inspector()
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_unique_object(table: str, name: str, columns: list[str]) -> bool:
    inspector = _inspector()
    if table not in inspector.get_table_names():
        return False

    for constraint in inspector.get_unique_constraints(table):
        cols = list(constraint.get("column_names") or [])
        if constraint.get("name") == name or cols == columns:
            return True

    # PostgreSQL may expose a unique constraint's backing object as an index.
    for index in inspector.get_indexes(table):
        cols = list(index.get("column_names") or [])
        if index.get("unique") and (index.get("name") == name or cols == columns):
            return True

    return False


def _create_unique_constraint_if_missing(
    table: str, name: str, columns: list[str]
) -> None:
    if not _has_unique_object(table, name, columns):
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_unique_constraint(name, columns)


def upgrade() -> None:
    # 1. Add tenant columns.
    for table, column in (
        ("patterns", "tenant_id"),
        ("agent_reliability", "tenant_id"),
        ("predictions", "tenant_id"),
    ):
        if not _has_column(table, column):
            op.add_column(
                table,
                sa.Column("tenant_id", sa.String(length=64), nullable=True),
            )
            op.create_index(
                f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False
            )

    # 2. Drop the globally-unique idempotency indexes.
    inspector = _inspector()
    for table in ("incidents", "investigation_jobs"):
        if table not in inspector.get_table_names():
            continue
        for index in inspector.get_indexes(table):
            unique_cols = [
                c for c in (index.get("column_names") or []) if c
            ]
            if index.get("unique") and unique_cols == ["idempotency_key"]:
                op.drop_index(index["name"], table_name=table)

    # 3. Re-establish uniqueness per tenant. The initial schema may already
    # have these exact composite constraints, so do not recreate them.
    _create_unique_constraint_if_missing(
        "incidents",
        "_tenant_incident_idem_uc",
        ["tenant_id", "idempotency_key"],
    )
    _create_unique_constraint_if_missing(
        "investigation_jobs",
        "_tenant_job_idem_uc",
        ["tenant_id", "idempotency_key"],
    )

    # 4. Predictions were previously unique on (service, failure_type)
    # globally; that is now per tenant.
    inspector = _inspector()
    for index in inspector.get_indexes("predictions"):
        cols = [c for c in (index.get("column_names") or []) if c]
        if index.get("unique") and set(cols) == {
            "service",
            "predicted_failure_type",
        }:
            op.drop_index(index["name"], table_name="predictions")

    _create_unique_constraint_if_missing(
        "predictions",
        "_tenant_service_failure_uc",
        ["tenant_id", "service", "predicted_failure_type"],
    )

    # 5. Agent reliability was keyed by agent name alone, which is
    # incompatible with per-tenant rows.
    _create_unique_constraint_if_missing(
        "agent_reliability",
        "_tenant_agent_uc",
        ["tenant_id", "agent_name"],
    )

    # 6. users.email was globally unique; make it tenant-scoped.
    inspector = _inspector()
    for index in inspector.get_indexes("users"):
        cols = [c for c in (index.get("column_names") or []) if c]
        if index.get("unique") and cols == ["email"]:
            op.drop_index(index["name"], table_name="users")

    _create_unique_constraint_if_missing(
        "users",
        "_tenant_email_uc",
        ["tenant_id", "email"],
    )


def downgrade() -> None:
    # Only drop objects that this migration actually owns by name. A fresh
    # database may have received the same objects from the initial metadata
    # migration, in which case removing them here would damage the schema.
    inspector = _inspector()
    for table, name in (
        ("users", "_tenant_email_uc"),
        ("agent_reliability", "_tenant_agent_uc"),
        ("predictions", "_tenant_service_failure_uc"),
        ("investigation_jobs", "_tenant_job_idem_uc"),
        ("incidents", "_tenant_incident_idem_uc"),
    ):
        if table not in inspector.get_table_names():
            continue
        if any(c.get("name") == name for c in inspector.get_unique_constraints(table)):
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_constraint(name, type_="unique")

    for table in ("predictions", "agent_reliability", "patterns"):
        if _has_column(table, "tenant_id"):
            for index in _inspector().get_indexes(table):
                if index.get("name") == f"ix_{table}_tenant_id":
                    op.drop_index(index["name"], table_name=table)
            op.drop_column(table, "tenant_id")

    for table in ("incidents", "investigation_jobs"):
        if table in _inspector().get_table_names():
            indexes = _inspector().get_indexes(table)
            if not any(
                i.get("unique")
                and [c for c in (i.get("column_names") or []) if c]
                == ["idempotency_key"]
                for i in indexes
            ):
                op.create_index(
                    f"uq_{table}_idempotency_key",
                    table,
                    ["idempotency_key"],
                    unique=True,
                )
