from __future__ import annotations

from typing import Any

from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from app.bot.context import BotContext
from app.bot.factory import build_bot, build_dispatcher
from app.bot.storage import MemoryKV
from app.config import ChainsConfig, Settings
from app.web.__main__ import TELEGRAM_PATH, build_app


async def _client(settings: Settings, chains: ChainsConfig) -> TestClient[Any, Any]:
    settings = settings.model_copy(
        update={
            "bot_token": SecretStr("42:TEST"),
            "telegram_webhook_secret": SecretStr("s3cret"),
        }
    )
    ctx = BotContext(settings=settings, chains=chains, sessionmaker=None, kv=MemoryKV())  # type: ignore[arg-type]
    bot = build_bot(settings)
    app = build_app(settings, bot, build_dispatcher(ctx, MemoryStorage()))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_healthz(settings: Settings, chains: ChainsConfig) -> None:
    client = await _client(settings, chains)
    try:
        response = await client.get("/healthz")
        assert response.status == 200
        assert await response.json() == {"ok": True}
    finally:
        await client.close()


async def test_webhook_rejects_requests_without_secret(settings: Settings, chains: ChainsConfig) -> None:
    client = await _client(settings, chains)
    try:
        response = await client.post(TELEGRAM_PATH, json={"update_id": 1})
        assert response.status == 401
        forged = await client.post(
            TELEGRAM_PATH, json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        )
        assert forged.status == 401
    finally:
        await client.close()
