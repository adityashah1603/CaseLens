"""Alembic environment for the CaseLens API database.

The database URL comes from the `CASELENS_DATABASE_URL` environment variable (a SQLAlchemy URL
using the `postgresql+psycopg` driver). A caller may instead inject `sqlalchemy.url` into the
Alembic config programmatically. No URL or credential is ever stored in `alembic.ini`.

There is no ORM metadata: migrations are hand-written SQL (`op.execute`), so autogenerate is
intentionally unsupported.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

DATABASE_URL_ENV = "CASELENS_DATABASE_URL"

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str | None:
    injected = config.get_main_option("sqlalchemy.url")
    if injected:
        return injected
    return os.environ.get(DATABASE_URL_ENV) or None


def run_migrations_offline() -> None:
    """Emit SQL to stdout. Works without a URL (uses the PostgreSQL dialect)."""
    url = _database_url()
    if url is not None:
        context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    else:
        context.configure(
            dialect_name="postgresql", literal_binds=True, dialect_opts={"paramstyle": "named"}
        )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database in a single transaction."""
    url = _database_url()
    if url is None:
        raise RuntimeError(f"Set {DATABASE_URL_ENV} to a postgresql+psycopg:// URL.")
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
