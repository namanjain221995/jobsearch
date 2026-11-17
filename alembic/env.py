"""Alembic environment (async engine, asyncpg). The database URL comes from DATABASE_URL / .env,
or from ALEMBIC_DATABASE_URL when a script needs to point at another database (the test suite does)."""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.portal.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def database_url() -> str:
    return os.environ.get("ALEMBIC_DATABASE_URL") or config.get_main_option("sqlalchemy.url") or get_settings().database_url


def include_object(obj, name, type_, reflected, compare_to):
    # The crawler index tables are managed with raw SQL in the migrations, not by the ORM metadata.
    return not (type_ == "table" and name.startswith("index_"))


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
