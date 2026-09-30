"""tenant scoping for patterns, reliability, predictions and idempotency keys

Revision ID: b7c1e4a9d210
Revises: 0f52a83618c9
Create Date: 2026-09-30

Motivation
----------
``patterns``, ``agent_reliability`` and ``predictions`` had no tenant column at
all, so their contents were global: any authenticated caller could read another
workspace's failure signatures, agent telemetry and forecasts, and
``prediction.engine.predict()`` executed an unscoped
``DELETE FROM predictions`` that destroyed every other tenant's rows.

``incidents.idempotency_key`` and ``investigation_jobs.idempotency_key`` were
*globally* unique, which let one workspace permanently squat a key and deny it
to every other workspace.

Idempotency-key uniqueness becomes per tenant. The unique constraints are
rebuilt as composite so the same key may legitimately exist in two workspaces.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1e4a9d210"
down_revision: str | None = "0f52a83618c9"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    # 1. Add tenant columns.
    for table, column in (
        ("patterns", "tenant_id"),
        ("agent_reliability", "tenant_id"),
        ("predictions", "tenant_id"),
    ):
        if not _has_column(table, column):
            op.add_column(table, sa.Column("tenant_id", sa.String(length=64), nullable=True))
            op.create_index(
                f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False
            )

    # 2. Drop the globally-unique idempotency indexes.
    for table in ("incidents", "investigation_jobs"):
        inspector = sa.inspect(op.get_bind())
        if table not in inspector.get_table_names():
            continue
        for index in inspector.get_indexes(table):
            unique_cols = [c for c in (index.get("column_names") or []) if c]
            if index.get("unique") and unique_cols == ["idempotency_key"]:
                op.drop_index(index["name"], table_name=table)

    # 3. Re-establish uniqueness per tenant.
    op.create_unique_constraint(
        "_tenant_incident_idem_uc", "incidents", ["tenant_id", "idempotency_key"]
    )
    op.create_unique_constraint(
        "_tenant_job_idem_uc", "investigation_jobs", ["tenant_id", "idempotency_key"]
    )

    # 4. Predictions were previously unique on (service, failure_type) globally;
    #    that is now per tenant.
    inspector = sa.inspect(op.get_bind())
    for index in inspector.get_indexes("predictions"):
        cols = [c for c in (index.get("column_names") or []) if c]
        if index.get("unique") and set(cols) == {"service", "predicted_failure_type"}:
            op.drop_index(index["name"], table_name="predictions")
    op.create_unique_constraint(
        "_tenant_service_failure_uc",
        "predictions",
        ["tenant_id", "service", "predicted_failure_type"],
    )

    # 5. Agent reliability was keyed by agent name alone, which is incompatible
    #    with per-tenant rows. Re-key it as (tenant_id, agent_name).
    op.create_unique_constraint("_tenant_agent_uc", "agent_reliability", ["tenant_id", "agent_name"])

    # 6. ``users.email`` was globally unique, which let any workspace admin
    #    permanently claim an address and deny it to every other workspace.
    inspector = sa.inspect(op.get_bind())
    for index in inspector.get_indexes("users"):
        cols = [c for c in (index.get("column_names") or []) if c]
        if index.get("unique") and cols == ["email"]:
            op.drop_index(index["name"], table_name="users")
    op.create_unique_constraint("_tenant_email_uc", "users", ["tenant_id", "email"])


def downgrade() -> None:
    op.drop_constraint("_tenant_email_uc", "users", type_="unique")
    op.drop_constraint("_tenant_agent_uc", "agent_reliability", type_="unique")
    op.drop_constraint("_tenant_service_failure_uc", "predictions", type_="unique")
    op.drop_constraint("_tenant_job_idem_uc", "investigation_jobs", type_="unique")
    op.drop_constraint("_tenant_incident_idem_uc", "incidents", type_="unique")

    for table in ("predictions", "agent_reliability", "patterns"):
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_column(table, "tenant_id")

    for table in ("incidents", "investigation_jobs"):
        op.create_index(
            f"uq_{table}_idempotency_key",
            table,
            ["idempotency_key"],
            unique=True,
        )
