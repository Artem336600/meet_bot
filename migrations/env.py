from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import pool, create_engine
from sqlalchemy.engine import Connection, Engine

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Add your model's MetaData object here
# for 'autogenerate' support
from app.db.models import Base  # noqa: E402

target_metadata = Base.metadata


def get_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    # Для alembic используем СИНХРОННЫЙ драйвер psycopg2
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = "postgresql+psycopg2://" + database_url[len("postgresql+asyncpg://"):]
    elif database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg2://" + database_url[len("postgres://"):]
    elif database_url.startswith("postgresql://") and not database_url.startswith("postgresql+psycopg2://"):
        database_url = "postgresql+psycopg2://" + database_url[len("postgresql://"):]
    return database_url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable: Engine = create_engine(get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


