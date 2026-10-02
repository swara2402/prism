"""Add tenant-scoped WayPoint service configuration.

Stores workspace LLM credentials/configuration and customer-to-canonical
schema mappings. Secrets are encrypted by the application before persistence.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3d9f7a12e44"
down_revision = "b7c1e4a9d210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workspace_configs" in inspector.get_table_names():
        return

    op.create_table(
        "workspace_configs",
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("llm_provider", sa.String(length=32), nullable=False, server_default="ollama"),
        sa.Column("llm_model", sa.String(length=256), nullable=False, server_default="llama3.1:8b"),
        sa.Column("llm_base_url", sa.String(length=512), nullable=True),
        sa.Column("llm_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("schema_mapping", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("workspace_configs")
