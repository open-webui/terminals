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
    fileConfig(config.config_file_name)

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


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with an async engine."""
    url = _get_url()
    _ensure_sqlite_dir(url)

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — delegates to async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
