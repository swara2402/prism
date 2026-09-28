"""add tenant-bound service accounts

Revision ID: 20260928_service_accounts
Revises: 0f52a83618c9
"""
from alembic import op
import sqlalchemy as sa

revision = "20260928_service_accounts"
down_revision = "0f52a83618c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="engineer"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_service_accounts_token_hash"),
    )
    op.create_index("ix_service_accounts_tenant_id", "service_accounts", ["tenant_id"])
    op.create_index("ix_service_accounts_token_hash", "service_accounts", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_service_accounts_token_hash", table_name="service_accounts")
    op.drop_index("ix_service_accounts_tenant_id", table_name="service_accounts")
    op.drop_table("service_accounts")
