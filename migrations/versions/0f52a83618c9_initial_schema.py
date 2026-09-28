"""PRISM initial schema.

Revision ID: 0f52a83618c9
Revises:

The original explicit migration was accidentally removed from main. This
replacement restores the migration contract from the current declarative ORM
metadata, including identity and tenant tables.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from database.session import Base
from database import models as _models  # noqa: F401
from database import auth_models as _auth_models  # noqa: F401

revision: str = "0f52a83618c9"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the complete PRISM schema on a fresh database."""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Remove the complete PRISM schema in dependency-safe order."""
    Base.metadata.drop_all(bind=op.get_bind())
