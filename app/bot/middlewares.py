"""Outer middlewares, in order: update dedupe → user upsert/ban check → rate limit."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject, Update
from aiogram.types import User as TgUser

from app.bot import texts as T
from app.bot.context import BotContext
from app.bot.storage import KeyValueStore
from app.logging import get_logger
from app.services.users import is_blacklisted, touch_user

log = get_logger(__name__)
Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

UPDATE_DEDUPE_TTL = 24 * 3600


class DedupeMiddleware(BaseMiddleware):
    """Telegram re-delivers an update if our webhook did not answer in time: process it once."""

    def __init__(self, kv: KeyValueStore) -> None:
        self._kv = kv

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, Update) and not await self._kv.set_nx(
            f"upd:{event.update_id}", "1", UPDATE_DEDUPE_TTL
        ):
            log.info("duplicate_update_skipped", update_id=event.update_id)
            return None
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Keeps the users table fresh (username history!) and stops banned users early."""

    def __init__(self, ctx: BotContext) -> None:
        self._ctx = ctx

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        async with self._ctx.uow() as uow:
            user = await touch_user(
                uow.session,
                user_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name or "",
                language_code=tg_user.language_code,
                now=self._ctx.clock(),
            )
            banned = user.is_banned or await is_blacklisted(uow.session, user_id=user.id)
        if banned:
            await _reply(event, T.BANNED)
            return None
        data["user"] = user
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Fixed-window rate limit per user (protects the bot and the database from floods)."""

    def __init__(self, kv: KeyValueStore, *, limit: int = 30, window: int = 60) -> None:
        self._kv = kv
        self._limit = limit
        self._window = window

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None:
            count = await self._kv.incr(f"rl:{tg_user.id}", self._window)
            if count > self._limit:
                if count == self._limit + 1:
                    await _reply(event, T.RATE_LIMITED)
                log.info("rate_limited", user_id=tg_user.id, count=count)
                return None
        return await handler(event, data)


async def _reply(event: TelegramObject, text: str) -> None:
    inner = event.event if isinstance(event, Update) else event
    if isinstance(inner, CallbackQuery):
        await inner.answer(text, show_alert=True)
    elif isinstance(inner, Message):
        await inner.answer(text)
    elif isinstance(inner, InlineQuery):
        await inner.answer([], cache_time=0, is_personal=True)
