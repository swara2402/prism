"""
migrations.env
==============

Alembic environment wiring.

The script points at PRISM's *sync* database URL from ``config.settings`` so a
single env-config drives both the app and its migrations.  Production upgrades
are applied with ``alembic upgrade head``; ``init_db().create_all`` remains the
dev/test convenience path.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from config.settings import settings

sys.path.insert(0, ".")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Load every ORM model so ``Base.metadata`` is complete for autogenerate.
from database import models  # noqa: E402,F401
from database import auth_models  # noqa: E402,F401

# Point Alembic at the configured sync URL (never the hard-coded placeholder).
config.set_main_option("sqlalchemy.url", settings.database_sync_url)

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()