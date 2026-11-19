"""Production entry point: aiohttp server with the Telegram webhook (``python -m app.web``).

Updates are processed *before* answering Telegram (``handle_in_background=False``): if processing
fails, Telegram re-delivers the update and the dedupe middleware makes the retry safe.
The CryptoBot webhook is added to the same app in the CryptoBot module.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.factory import build_bot, build_dispatcher
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime

TELEGRAM_PATH = "/tg/webhook"
log = get_logger(__name__)


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app(settings: Settings, bot: Bot, dp: Dispatcher) -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=False,
        secret_token=settings.telegram_webhook_secret.get_secret_value() or None,
    ).register(app, path=TELEGRAM_PATH)
    setup_application(app, dp, bot=bot)
    return app


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    if not settings.webhook_base_url or not settings.telegram_webhook_secret.get_secret_value():
        raise SystemExit("WEBHOOK_BASE_URL and TELEGRAM_WEBHOOK_SECRET are required for webhook mode")
    runtime = await build_runtime(settings)
    bot = build_bot(settings)
    dp = build_dispatcher(runtime.ctx, runtime.fsm_storage)
    app = build_app(settings, bot, dp)
    await bot.set_webhook(
        url=settings.webhook_base_url.rstrip("/") + TELEGRAM_PATH,
        secret_token=settings.telegram_webhook_secret.get_secret_value(),
        allowed_updates=dp.resolve_used_update_types(),
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)  # noqa: S104 - inside the container
    await site.start()
    log.info("webhook_server_started", port=8080)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
