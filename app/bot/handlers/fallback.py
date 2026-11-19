"""Last router: anything nobody else handled."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message


async def on_unknown_text(message: Message) -> None:
    await message.answer("Не понял сообщение. Создать сделку — /new, меню — /menu, помощь — /help.")


async def on_unknown_button(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела. Откройте меню: /menu", show_alert=True)


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="fallback")
    router.message.register(on_unknown_text, F.text)
    router.callback_query.register(on_unknown_button)
    return router
