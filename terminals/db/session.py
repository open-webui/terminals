"""Async SQLAlchemy engine and session factory."""

import os
from pathlib import Path

from terminals.config import settings
from terminals.db.urls import async_database_url

engine = None
async_session = None

_db_url = async_database_url(settings.database_url)
if _db_url:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    # Ensure the directory for the SQLite file exists.
    if _db_url.startswith("sqlite"):
        _db_path = _db_url.split("///", 1)[-1]
        if _db_path:
            os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)

    engine = create_async_engine(_db_url, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def init_db():
    """Run Alembic migrations (sync — safe to call from any context)."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config()
    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    if ini_path.exists():
        alembic_cfg = Config(str(ini_path))

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    alembic_cfg.set_main_option("script_location", str(migrations_dir))

    from sqlalchemy import create_engine, inspect, text
    from terminals.db.urls import sync_database_url
    check_engine = create_engine(sync_database_url(settings.database_url))
    try:
        with check_engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            if "alembic_version" in tables:
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "004_templates":
                    raise RuntimeError("Pre-0.4 database detected. Follow the manual 0.4 migration in CHANGELOG.md using a new database and separate storage; keep this database unchanged.")
            elif tables:
                raise RuntimeError("Existing unversioned database requires explicit migration")
    finally:
        check_engine.dispose()
    command.upgrade(alembic_cfg, "head")


async def close_db():
    """Dispose of the engine's connection pool."""
    if engine is not None:
        await engine.dispose()
