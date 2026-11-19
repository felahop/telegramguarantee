"""Process-level wiring: engine, Redis, stores, bot context."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot.context import BotContext
from app.bot.storage import RedisKV
from app.config import Settings, get_chains
from app.db.session import create_engine, create_sessionmaker
from app.services.users import ensure_admins

WIZARD_STATE_TTL = 3600


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    ctx: BotContext
    fsm_storage: RedisStorage

    async def close(self) -> None:
        await self.fsm_storage.close()
        await self.redis.aclose()
        await self.engine.dispose()


async def build_runtime(settings: Settings) -> Runtime:
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
    async with sessionmaker() as session, session.begin():
        await ensure_admins(session, settings.admin_ids)
    return Runtime(settings=settings, engine=engine, redis=redis, ctx=ctx, fsm_storage=fsm_storage)
