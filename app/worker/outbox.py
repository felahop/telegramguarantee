"""Outbox delivery: Telegram notifications written by services, sent here with retries.

Delivery is at-least-once: a crash between ``sendMessage`` and the commit re-sends the message.
For notifications that is the right trade-off (never "status changed and nobody was told").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import sqlalchemy as sa
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.bot.context import BotContext
from app.bot.notices import render_notice
from app.db.models import NotificationOutbox, User
from app.domain.enums import OutboxStatus
from app.logging import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 8
GLOBAL_PACE_SECONDS = 1 / 25  # Telegram allows ~30 messages/s per bot
IDLE_POLL_SECONDS = 1.0


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** max(attempts - 1, 0), 3600))


class OutboxSender:
    def __init__(self, ctx: BotContext, bot: Bot, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self._ctx = ctx
        self._bot = bot
        self._max_attempts = max_attempts

    async def send_one(self) -> bool:
        """Deliver the oldest due notification. Returns False when nothing is due."""
        now = self._ctx.clock()
        async with self._ctx.sessionmaker() as session, session.begin():
            row = (
                await session.execute(
                    sa.select(NotificationOutbox)
                    .where(
                        NotificationOutbox.status == OutboxStatus.PENDING,
                        NotificationOutbox.next_attempt_at <= now,
                    )
                    .order_by(NotificationOutbox.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.attempts += 1
            try:
                rendered = await render_notice(self._ctx, session, row)
                if rendered is None:
                    row.status = OutboxStatus.SKIPPED
                    row.last_error = "obsolete"
                elif rendered.inline_message_id:
                    await self._bot.edit_message_text(
                        text=rendered.text, inline_message_id=rendered.inline_message_id
                    )
                    row.status = OutboxStatus.SENT
                else:
                    message = await self._bot.send_message(
                        row.user_id, rendered.text, reply_markup=rendered.markup
                    )
                    row.status = OutboxStatus.SENT
                    row.tg_message_id = message.message_id
            except TelegramRetryAfter as exc:
                row.attempts -= 1  # flood control is not the message's fault
                row.next_attempt_at = now + timedelta(seconds=exc.retry_after)
                log.warning("outbox_flood_control", retry_after=exc.retry_after)
            except TelegramForbiddenError as exc:
                # The user blocked the bot or never started it: nothing to retry.
                row.status = OutboxStatus.SKIPPED
                row.last_error = str(exc)[:500]
                await session.execute(sa.update(User).where(User.id == row.user_id).values(bot_blocked=True))
            except TelegramBadRequest as exc:
                permanent = "chat not found" in str(exc) or "message to edit not found" in str(exc)
                self._fail(row, exc, now, permanent=permanent or row.attempts >= 2)
            except (TelegramNetworkError, TelegramServerError, TimeoutError) as exc:
                self._fail(row, exc, now, permanent=False)
            except Exception as exc:  # rendering bug etc.: keep the row, alert via logs
                log.error("outbox_unexpected_error", outbox_id=row.id, error=repr(exc), exc_info=exc)
                self._fail(row, exc, now, permanent=False)
        return True

    def _fail(self, row: NotificationOutbox, exc: Exception, now: datetime, *, permanent: bool) -> None:
        row.last_error = repr(exc)[:500]
        if permanent or row.attempts >= self._max_attempts:
            row.status = OutboxStatus.FAILED
            log.warning("outbox_failed", outbox_id=row.id, kind=row.kind, error=row.last_error)
        else:
            row.next_attempt_at = now + _backoff(row.attempts)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("outbox_sender_started")
        while not stop.is_set():
            try:
                sent = await self.send_one()
            except Exception as exc:  # database hiccup: wait and continue
                log.error("outbox_loop_error", error=repr(exc), exc_info=exc)
                sent = False
            delay = GLOBAL_PACE_SECONDS if sent else IDLE_POLL_SECONDS
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
