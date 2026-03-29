"""Alembic async environment configuration.

Reads the database URL from ``terminals.config.settings`` so the
connection string is never hardcoded in ``alembic.ini``.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from terminals.config import settings

# Import *all* models so Base.metadata is fully populated.
from terminals.models.base import Base  # noqa: F401
from terminals.models.policy import Policy  # noqa: F401

# Alembic Config object — gives access to alembic.ini values.
config = context.config

# Set up Python logging from the INI file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata for autogenerate support.
target_metadata = Base.metadata


def _get_url() -> str:
    """Return the DB URL, converting async drivers for offline mode."""
    return settings.database_url


def _sync_url(url: str) -> str:
    """Convert an async URL to a sync one for offline migrations."""
    return (
        url.replace("sqlite+aiosqlite", "sqlite")
        .replace("postgresql+asyncpg", "postgresql")
    )


def _ensure_sqlite_dir(url: str) -> None:
    """Create parent directories for SQLite database files."""
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path:
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without connecting)."""
    url = _sync_url(_get_url())
    _ensure_sqlite_dir(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a sync engine.

    Alembic is called from within an already-running async event loop
    (via ``asyncio.to_thread``), so we use a plain sync engine here to
    avoid nested-event-loop issues with aiosqlite/asyncpg.
    """
    from sqlalchemy import create_engine

    url = _sync_url(_get_url())
    _ensure_sqlite_dir(url)

    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
