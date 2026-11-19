"""Bot and dispatcher assembly (shared by polling, webhook and tests)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage

from app.bot.context import BotContext
from app.bot.errors import on_error
from app.bot.handlers import commands, deal_card, fallback, inline, start, wizard
from app.bot.middlewares import DedupeMiddleware, ThrottleMiddleware, UserMiddleware
from app.config import Settings


def build_bot(settings: Settings, *, session: BaseSession | None = None) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher(
    ctx: BotContext, storage: BaseStorage, *, rate_limit: int = 30, rate_window: int = 60
) -> Dispatcher:
    dp = Dispatcher(storage=storage, ctx=ctx)
    dp.update.outer_middleware(DedupeMiddleware(ctx.kv))
    dp.update.outer_middleware(UserMiddleware(ctx))
    dp.update.outer_middleware(ThrottleMiddleware(ctx.kv, limit=rate_limit, window=rate_window))
    # Order matters: commands work in any wizard state; the fallback router goes last.
    dp.include_routers(
        start.build_router(),
        commands.build_router(),
        inline.build_router(),
        deal_card.build_router(),
        wizard.build_router(),
        fallback.build_router(),
    )
    dp.errors.register(on_error)
    return dp
