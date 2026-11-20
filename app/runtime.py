"""Process-level wiring: engine, Redis, stores, bot context."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot.context import BotContext
from app.bot.storage import RedisKV
from app.chains.registry import check_supported
from app.config import Settings, get_chains
from app.db.session import create_engine, create_sessionmaker
from app.domain.enums import Network, Provider
from app.providers.cryptobot.client import CryptoPayClient
from app.providers.cryptobot.gateway import CryptoBotGateway
from app.providers.onchain.gateway import OnchainGateway
from app.services.deals import PaymentGateway
from app.services.gateways import RoutingGateway
from app.services.users import ensure_admins

WIZARD_STATE_TTL = 3600


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    ctx: BotContext
    fsm_storage: RedisStorage
    cryptopay: CryptoPayClient | None = None

    async def close(self) -> None:
        if self.cryptopay is not None:
            await self.cryptopay.close()
        await self.fsm_storage.close()
        await self.redis.aclose()
        await self.engine.dispose()


async def build_runtime(settings: Settings) -> Runtime:
    check_supported(get_chains())  # never accept payments in a network nobody watches
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url)
    ctx = BotContext(settings=settings, chains=get_chains(), sessionmaker=sessionmaker, kv=RedisKV(redis))
    fsm_storage = RedisStorage(
        redis,
        key_builder=DefaultKeyBuilder(prefix="escrow:fsm"),
        state_ttl=WIZARD_STATE_TTL,
        data_ttl=WIZARD_STATE_TTL,
    )
    gateways: dict[Network, PaymentGateway] = {}
    cryptopay = None
    if settings.cryptobot_api_token.get_secret_value() and Network.CRYPTOBOT in ctx.chains.enabled_networks():
        cryptopay = CryptoPayClient(settings.cryptobot_api_token, testnet=settings.cryptobot_testnet)
        gateways[Network.CRYPTOBOT] = CryptoBotGateway(cryptopay, settings, ctx.chains, clock=ctx.clock)
    onchain = OnchainGateway(settings, ctx.chains, clock=ctx.clock)
    for network in ctx.chains.enabled_networks():
        if network.provider is Provider.ONCHAIN:
            gateways[network] = onchain
    ctx.gateway = RoutingGateway(gateways)
    ctx.cryptopay = cryptopay
    async with sessionmaker() as session, session.begin():
        await ensure_admins(session, settings.admin_ids)
    return Runtime(
        settings=settings, engine=engine, redis=redis, ctx=ctx, fsm_storage=fsm_storage, cryptopay=cryptopay
    )
