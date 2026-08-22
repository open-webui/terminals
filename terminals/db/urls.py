"""Database URL normalization helpers."""


def async_database_url(url: str) -> str:
    """Return a URL suitable for SQLAlchemy's async engine."""
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return url


def sync_database_url(url: str) -> str:
    """Return a URL suitable for Alembic's sync migration engine."""
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url.removeprefix("sqlite+aiosqlite://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url
