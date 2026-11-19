"""Integration fixtures: a real PostgreSQL (row locks, partial unique indexes and triggers are part of
the money guarantees — SQLite would make these tests meaningless).

Point ``TEST_DATABASE_URL`` at an empty database; the schema is rebuilt from the Alembic migrations
(upgrade → downgrade → upgrade, so downgrades are exercised too).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base
from tests.conftest import ROOT

DEFAULT_TEST_DB = "postgresql+asyncpg://postgres@127.0.0.1:54329/escrow_test"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)

    async def _reset() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset())
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, poolclass=NullPool)
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with eng.begin() as conn:
        # Replica mode skips the append-only/no-truncate triggers for test cleanup only.
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s
