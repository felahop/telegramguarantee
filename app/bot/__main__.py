"""Development entry point: long polling (``python -m app.bot``). Production uses the webhook (app.web)."""

from __future__ import annotations

import asyncio

from app.bot.factory import build_bot, build_dispatcher
from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    runtime = await build_runtime(settings)
    bot = build_bot(settings)
    dp = build_dispatcher(runtime.ctx, runtime.fsm_storage)
    get_logger(__name__).info("polling_started", bot=settings.bot_username)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
