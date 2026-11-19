"""Global error handler: domain errors become friendly messages; everything else is logged."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

from app.bot import texts as T
from app.domain.errors import DomainError
from app.logging import get_logger

log = get_logger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    update = event.update
    if isinstance(exc, DomainError):
        text = T.error_text(exc)
        log.info("domain_error", code=exc.code, message=exc.message, update_id=update.update_id)
    elif isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
        if update.callback_query:
            await update.callback_query.answer()
        return True
    else:
        text = T.UNEXPECTED_ERROR
        log.error("unhandled_error", update_id=update.update_id, error=repr(exc), exc_info=exc)
    if update.callback_query:
        await update.callback_query.answer(_plain(text), show_alert=True)
    elif update.message:
        await update.message.answer(text)
    elif update.inline_query:
        await update.inline_query.answer([], cache_time=0, is_personal=True)
    return True


def _plain(text: str) -> str:
    """Callback alerts do not render HTML and are limited to 200 characters."""
    for tag in ("<code>", "</code>", "<b>", "</b>", "<i>", "</i>"):
        text = text.replace(tag, "")
    return text[:200]
