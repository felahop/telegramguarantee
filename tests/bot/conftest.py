from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.storage import MemoryKV
from app.config import ChainsConfig, Settings
from app.domain.enums import Network
from app.providers.cryptobot.client import CryptoPayClient
from app.providers.cryptobot.gateway import CryptoBotGateway
from app.services.gateways import RoutingGateway
from tests.bot.harness import BotHarness, TgUser
from tests.cryptobot_fake import TOKEN, FakeCryptoPay
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


@pytest.fixture
def fake() -> FakeCryptoPay:
    return FakeCryptoPay()


@pytest.fixture
async def cryptopay(ctx: BotContext, fake: FakeCryptoPay) -> AsyncIterator[CryptoPayClient]:
    """The bot context talking to an in-memory Crypto Pay."""
    client = CryptoPayClient(SecretStr(TOKEN), testnet=True, transport=fake.transport())
    ctx.settings = ctx.settings.model_copy(update={"cryptobot_api_token": SecretStr(TOKEN)})
    ctx.gateway = RoutingGateway(
        {Network.CRYPTOBOT: CryptoBotGateway(client, ctx.settings, ctx.chains, clock=ctx.clock)}
    )
    yield client
    await client.close()


@pytest.fixture
def paying_bot(ctx: BotContext, cryptopay: CryptoPayClient) -> BotHarness:
    return BotHarness.create(ctx)
