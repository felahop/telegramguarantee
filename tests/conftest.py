from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import ChainsConfig, Settings, load_chains
from app.db.base import Base
from tests import addresses

ROOT = Path(__file__).resolve().parent.parent
TEST_FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="  # test-only Fernet key


@pytest.fixture(scope="session")
def chains() -> ChainsConfig:
    """chains.example.yaml with every network enabled (deposit addresses are test values)."""
    base = load_chains(ROOT / "chains.example.yaml")
    deposit = {
        "tron": addresses.tron(1000),
        "bsc": addresses.evm(1000),
        "eth": addresses.evm(1000),
        "ton": addresses.ton(1000),
        "sol": addresses.sol(1000),
    }
    networks = {
        network: cfg.model_copy(update={"enabled": True, "deposit_address": deposit.get(network.value, "")})
        for network, cfg in base.networks.items()
    }
    return ChainsConfig(networks=networks)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        fee_rate_bps=200,
        fee_min_usdt=Decimal("1.00"),
        deal_min_usdt=Decimal("5"),
        deal_max_usdt=Decimal("10000"),
        new_user_deal_max_usdt=Decimal("300"),
        manual_approval_threshold_usdt=Decimal("2000"),
        admin_totp_required=True,
        secrets_encryption_key=TEST_FERNET_KEY,
        offer_ttl=timedelta(hours=24),
        payment_ttl=timedelta(minutes=30),
    )


# ── database (real PostgreSQL; fixtures are lazy, unit tests never touch them) ──────────────
# Point TEST_DATABASE_URL at an empty database; the schema is rebuilt from the Alembic migrations
# (upgrade → downgrade → upgrade, so downgrades are exercised too). SQLite would make the tests
# meaningless: row locks, partial unique indexes and triggers are part of the money guarantees.
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
