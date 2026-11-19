from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.storage import MemoryKV
from app.config import ChainsConfig, Settings
from tests.bot.harness import BotHarness, TgUser
from tests.integration.deal_world import Clock

ALICE = TgUser(1001, "alice_buyer", "Alice")
BOB = TgUser(1002, "bob_seller", "Bob")
EVE = TgUser(1003, "eve_stranger", "Eve")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ctx(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, chains: ChainsConfig, clock: Clock
) -> BotContext:
    return BotContext(
        settings=settings.model_copy(update={"bot_username": "escrow_test_bot"}),
        chains=chains,
        sessionmaker=sessionmaker,
        kv=MemoryKV(),
        clock=clock,
    )


@pytest.fixture
def bot(ctx: BotContext) -> BotHarness:
    return BotHarness.create(ctx)
