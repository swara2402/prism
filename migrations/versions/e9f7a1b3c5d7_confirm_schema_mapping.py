"""Track customer schema mapping confirmation."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e9f7a1b3c5d7"
down_revision = "d8e4f6a7b901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("workspace_configs")}
    if "schema_mapping_confirmed_at" not in columns:
        op.add_column(
            "workspace_configs",
            sa.Column("schema_mapping_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("workspace_configs", "schema_mapping_confirmed_at")
