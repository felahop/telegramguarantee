from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401  (registers tables)
from app.config import get_settings
from app.db.base import Base, MinorAmount

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    """Keep migrations self-contained: app-level column types render as plain SQLAlchemy types."""
    if type_ == "type" and isinstance(obj, MinorAmount):
        return "sa.Numeric(precision=78, scale=0)"
    return False


def _url() -> str:
    # Allows tests / CI to point alembic at another database without touching .env.
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
