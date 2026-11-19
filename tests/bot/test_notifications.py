"""Outbox delivery and rendering of every notification kind."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerInlineQuery, EditMessageText, SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.notices import render_notice
from app.db.models import Deal, NotificationOutbox, User
from app.domain.enums import DealStatus, OutboxStatus, UserRole
from app.services.notifications import ADMIN_NOTICES, Notice, enqueue
from app.worker.outbox import OutboxSender
from app.worker.timers import run_due_timers
from tests.bot.conftest import ALICE, BOB
from tests.bot.harness import BotHarness
from tests.bot.test_bot_flows import deals, press, wizard_to_counterparty
from tests.integration.deal_world import Clock


async def drain(ctx: BotContext, harness: BotHarness) -> int:
    sender = OutboxSender(ctx, harness.bot)
    count = 0
    while await sender.send_one():
        count += 1
    return count


async def rows(sm: async_sessionmaker[AsyncSession]) -> list[NotificationOutbox]:
    async with sm() as s:
        return list(
            (await s.execute(sa.select(NotificationOutbox).order_by(NotificationOutbox.id))).scalars()
        )


async def make_offer(bot: BotHarness) -> None:
    await bot.send(BOB, "/start")
    await wizard_to_counterparty(bot, ALICE)
    await bot.send(ALICE, "@bob_seller")
    await press(bot, ALICE, "Создать сделку")


async def test_offer_reaches_counterparty_and_acceptance_reaches_creator(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_offer(bot)
    assert await drain(ctx, bot) == 1
    offer = bot.last_for(BOB.id)
    assert offer is not None
    assert "Вам предложили сделку" in offer.text
    assert "150.00 USDT" in offer.text
    # the button in the notification is the live one: Bob accepts right from it
    await bot.press(BOB, offer.data("Принять условия"))
    await drain(ctx, bot)
    assert "Условия приняты" in bot.last_text(ALICE.id)
    assert all(r.status is OutboxStatus.SENT for r in await rows(sessionmaker))


async def test_blocked_user_is_skipped_and_marked(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_offer(bot)
    bot.session.errors[BOB.id] = TelegramForbiddenError(
        method=SendMessage(chat_id=BOB.id, text="x"), message="Forbidden: bot was blocked by the user"
    )
    await drain(ctx, bot)
    [row] = await rows(sessionmaker)
    assert row.status is OutboxStatus.SKIPPED
    async with sessionmaker() as s:
        user = await s.get(User, BOB.id)
        assert user is not None
        assert user.bot_blocked is True
    del bot.session.errors[BOB.id]
    await bot.send(BOB, "/start")  # unblocking = starting the bot again
    async with sessionmaker() as s:
        assert (await s.get(User, BOB.id)).bot_blocked is False  # type: ignore[union-attr]


async def test_flood_control_reschedules_without_counting(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    await make_offer(bot)
    bot.session.errors[BOB.id] = TelegramRetryAfter(
        method=SendMessage(chat_id=BOB.id, text="x"), message="Too Many Requests", retry_after=7
    )
    await OutboxSender(ctx, bot.bot).send_one()
    [row] = await rows(sessionmaker)
    assert (row.status, row.attempts) == (OutboxStatus.PENDING, 0)
    assert row.next_attempt_at == clock.now + timedelta(seconds=7)
    assert not await OutboxSender(ctx, bot.bot).send_one()  # not due yet
    del bot.session.errors[BOB.id]
    clock.advance(timedelta(seconds=7))
    assert await drain(ctx, bot) == 1


async def test_network_errors_back_off_then_fail(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    await make_offer(bot)
    bot.session.errors[BOB.id] = TelegramNetworkError(
        method=SendMessage(chat_id=BOB.id, text="x"), message="down"
    )
    sender = OutboxSender(ctx, bot.bot, max_attempts=3)
    for _ in range(3):
        assert await sender.send_one()
        clock.advance(timedelta(hours=2))
    [row] = await rows(sessionmaker)
    assert (row.status, row.attempts) == (OutboxStatus.FAILED, 3)


async def test_inline_card_is_updated_on_acceptance(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(ALICE, "/start")
    await bot.send(BOB, "/start")
    calls = await bot.inline(ALICE, "@bob_seller 100 usdt cb Логотип")
    [answer] = [c for c in calls if isinstance(c, AnswerInlineQuery)]
    result = next(r for r in answer.results if r.id.startswith("buyer:"))
    await bot.chosen(ALICE, result.id, inline_message_id="INLINE-1")
    token = result.reply_markup.inline_keyboard[0][0].url.split("start=i_", 1)[1]  # type: ignore[union-attr]
    await bot.send(BOB, f"/start i_{token}")
    [deal] = await deals(sessionmaker)
    assert deal.inline_message_id == "INLINE-1"
    await press(bot, BOB, "Принять условия")
    await drain(ctx, bot)
    edits = [
        c for c in bot.session.calls if isinstance(c, EditMessageText) and c.inline_message_id == "INLINE-1"
    ]
    assert len(edits) == 1
    assert "принята" in edits[0].text


async def test_timers_produce_notifications(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    await make_offer(bot)
    await drain(ctx, bot)
    clock.advance(ctx.settings.offer_ttl)
    assert await run_due_timers(ctx) == 1
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.EXPIRED
    await drain(ctx, bot)
    assert "Сделка истекла" in bot.last_text(ALICE.id)


@pytest.mark.parametrize("kind", [k for k in Notice if k is not Notice.INLINE_CARD])
async def test_every_notice_renders(
    kind: Notice, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession], bot: BotHarness
) -> None:
    await make_offer(bot)
    payloads = {
        Notice.CONFIRM_COUNTERPARTY: {"user_id": BOB.id},
        Notice.PAYOUT_STARTED: {"amount": 98_000_000},
        Notice.REFUND_STARTED: {"amount": 1_000_000},
        Notice.DISPUTE_RESOLVED: {"decision": "split", "to_buyer": 1, "to_seller": 2, "comment": "<b>x</b>"},
        Notice.DISPUTE_OPENED: {"reason": "buyer_timeout"},
        Notice.DEAL_FUNDED: {"overpaid_to_seller": 500_000},
    }
    async with sessionmaker() as s, s.begin():
        [deal] = list((await s.execute(sa.select(Deal))).scalars())
        recipient = BOB.id
        if kind in ADMIN_NOTICES:
            await s.execute(sa.update(User).where(User.id == BOB.id).values(role=UserRole.ADMIN))
        await enqueue(
            s,
            user_id=recipient,
            kind=kind,
            deal_id=deal.id,
            dedupe_key=f"test:{kind}",
            now=ctx.clock(),
            payload={"status": deal.status.value, "code": deal.code, **payloads.get(kind, {})},
        )
    async with sessionmaker() as s:
        row = (
            await s.execute(
                sa.select(NotificationOutbox).where(NotificationOutbox.dedupe_key == f"test:{kind}")
            )
        ).scalar_one()
        rendered = await render_notice(ctx, s, row)
    assert rendered is not None
    assert rendered.text
    assert "<b>x</b>" not in rendered.text  # payload text is escaped
    if kind in ADMIN_NOTICES:
        assert rendered.markup is None  # admins get the admin panel, not party buttons
        assert deal.code in rendered.text
