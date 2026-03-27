"""CLI entry point for the Terminals orchestrator."""

import click
import uvicorn


@click.group()
def main():
    """terminals — multi-tenant terminal orchestrator"""
    pass


BANNER = r"""
 _____                   _             _
|_   _|                 (_)           | |
  | | ___ _ __ _ __ ___  _ _ __   __ _| |___
  | |/ _ | '__| '_ ` _ \| | '_ \ / _` | / __|
  | |  __| |  | | | | | | | | | | (_| | \__ \
  \_/\___|_|  |_| |_| |_|_|_| |_|\__,_|_|___/
"""


@main.command()
@click.option("--host", default=None, help="Bind host (overrides TERMINALS_HOST)")
@click.option("--port", default=None, type=int, help="Bind port (overrides TERMINALS_PORT)")
@click.option(
    "--api-key",
    default=None,
    help="Bearer API key (overrides TERMINALS_API_KEY)",
)
def serve(host: str | None, port: int | None, api_key: str | None):
    """Start the orchestrator API server."""
    import os
    import secrets

    from terminals.config import settings

    # CLI flags take precedence over env/config.
    effective_host = host or settings.host
    effective_port = port or settings.port

    if api_key is not None:
        os.environ["TERMINALS_API_KEY"] = api_key
        settings.api_key = api_key

    generated = not settings.api_key
    if generated:
        key = secrets.token_urlsafe(24)
        os.environ["TERMINALS_API_KEY"] = key
        settings.api_key = key

    click.echo(BANNER)
    if generated:
        click.echo("=" * 60)
        click.echo(f"  API Key: {settings.api_key}")
        click.echo("=" * 60)
    click.echo()

    uvicorn.run("terminals.main:app", host=effective_host, port=effective_port)


# ---------------------------------------------------------------------------
# Database / migration commands
# ---------------------------------------------------------------------------


@main.group()
def db():
    """Database migration commands (Alembic)."""
    pass


def _alembic_cfg():
    """Build an Alembic Config pointing at the right ini and script dir."""
    from pathlib import Path

    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent / "alembic.ini"
    if ini_path.exists():
        cfg = Config(str(ini_path))
    else:
        cfg = Config()

    # Always resolve script_location to an absolute path so the CLI works
    # regardless of the current working directory.
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parent / "migrations"),
    )

    from terminals.config import settings

    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


@db.command()
@click.option("--revision", default="head", help="Target revision (default: head)")
def upgrade(revision: str):
    """Run pending migrations."""
    from alembic import command

    click.echo(f"Upgrading database to {revision}…")
    command.upgrade(_alembic_cfg(), revision)
    click.echo("Done.")


@db.command()
@click.option("-m", "--message", required=True, help="Migration message")
@click.option("--autogenerate/--no-autogenerate", default=True, help="Auto-detect changes")
def revision(message: str, autogenerate: bool):
    """Create a new migration revision."""
    from alembic import command

    command.revision(_alembic_cfg(), message=message, autogenerate=autogenerate)
    click.echo("Revision created.")


@db.command()
def current():
    """Show current migration revision."""
    from alembic import command

    command.current(_alembic_cfg())


@db.command()
@click.argument("revision", default="head")
def stamp(revision: str):
    """Stamp the database as a given revision without running migrations."""
    from alembic import command

    command.stamp(_alembic_cfg(), revision)
    click.echo(f"Stamped as {revision}.")


if __name__ == "__main__":
    main()
