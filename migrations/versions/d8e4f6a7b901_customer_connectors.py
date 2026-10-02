"""Add tenant-scoped customer evidence connectors."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d8e4f6a7b901"
down_revision = "c3d9f7a12e44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "customer_connectors" in inspector.get_table_names():
        return
    op.create_table(
        "customer_connectors",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="http_json"),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="incidents"),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=True),
        sa.Column("http_method", sa.String(length=8), nullable=False, server_default="GET"),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=True),
        sa.Column("headers_encrypted", sa.Text(), nullable=True),
        sa.Column("payload_path", sa.String(length=512), nullable=True),
        sa.Column("mapping", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("schedule_seconds", sa.Integer(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_connectors_tenant_id", "customer_connectors", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_customer_connectors_tenant_id", table_name="customer_connectors")
    op.drop_table("customer_connectors")
