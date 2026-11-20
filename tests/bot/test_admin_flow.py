"""Admin panel end to end: TOTP setup, deciding a dispute, approving a payout, an unidentified payment."""

from __future__ import annotations

import re
from datetime import timedelta

import pyotp
import sqlalchemy as sa
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.admin_keyboards import AdmAct, AdmCb
from app.bot.callbacks import DealAct, DealCb
from app.bot.context import BotContext
from app.db.models import Deal, Payout, User
from app.domain.enums import DealStatus, PayoutKind, PayoutStatus, UserRole
from tests.bot.conftest import ALICE, BOB
from tests.bot.harness import BotHarness, TgUser
from tests.bot.test_bot_flows import deals, press
from tests.bot.test_execution_flow import funded
from tests.bot.test_notifications import drain
from tests.cryptobot_fake import FakeCryptoPay
from tests.integration.deal_world import Clock

ADMIN = TgUser(1009, "chief_admin", "Admin")


async def make_admin(bot: BotHarness, sm: async_sessionmaker[AsyncSession], clock: Clock) -> str:
    """Start the bot as ADMIN, grant the role, enroll TOTP. Returns the TOTP secret."""
    await bot.send(ADMIN, "/start")
    async with sm() as s, s.begin():
        await s.execute(sa.update(User).where(User.id == ADMIN.id).values(role=UserRole.ADMIN))
    await bot.send(ADMIN, "/totp")
    setup = [c for c in bot.session.calls if isinstance(c, SendMessage) and "Подключение TOTP" in c.text][-1]
    assert setup.protect_content is True  # the secret cannot be forwarded
    match = re.search(r"<code>([A-Z2-7]{16,})</code>", setup.text)
    assert match is not None
    secret = match.group(1)
    await bot.send(ADMIN, code(secret, clock))
    assert "TOTP подключён" in bot.last_text(ADMIN.id)
    clock.advance(timedelta(seconds=30))  # the enrollment code is spent
    return secret


def code(secret: str, clock: Clock) -> str:
    return pyotp.TOTP(secret).at(int(clock.now.timestamp()))


async def payouts(sm: async_sessionmaker[AsyncSession], deal_id: int) -> dict[PayoutKind, Payout]:
    async with sm() as s:
        return {
            p.kind: p for p in (await s.execute(sa.select(Payout).where(Payout.deal_id == deal_id))).scalars()
        }


async def test_admin_panel_is_staff_only(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/admin")
    assert "Не понял сообщение" in bot.last_text(ALICE.id)


async def test_decide_a_dispute_with_totp(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    clock: Clock,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.DISPUTE, id=deal.id, v=deal.version).pack())
    await bot.send(ALICE, "Получил только половину макетов")
    await bot.press(BOB, DealCb(a=DealAct.CHAT, id=deal.id, v=deal.version).pack())
    await bot.send(BOB, "Остальное отправлю завтра")
    await bot.send(BOB, "/done")
    secret = await make_admin(bot, sessionmaker, clock)

    await bot.send(ADMIN, "/admin")
    assert "Открытые споры: <b>1</b>" in bot.last_text(ADMIN.id)
    await press(bot, ADMIN, "Споры")
    await press(bot, ADMIN, deal.code)
    dossier = bot.last_text(ADMIN.id)
    assert "Получил только половину макетов" in dossier
    assert "Остальное отправлю завтра" in dossier
    assert "В escrow: <b>102.00 USDT</b>" in dossier

    await press(bot, ADMIN, "Взять в работу")
    assert "in_review" in bot.last_text(ADMIN.id)
    await drain(ctx, bot)
    assert "Арбитр взял спор в работу" in bot.last_text(ALICE.id)

    await press(bot, ADMIN, "Другой %")
    await bot.send(ADMIN, "70")
    preview = bot.last_text(ADMIN.id)
    assert "покупателю 70%" in preview
    assert "Покупателю: <b>70.00 USDT</b>" in preview
    assert "Продавцу: <b>30.00 USDT</b>" in preview
    await press(bot, ADMIN, "Подтвердить решение")
    await bot.send(ADMIN, "Передана половина работы — делим 70/30")
    assert "Введите 6-значный код" in bot.last_text(ADMIN.id)

    await bot.send(ADMIN, "000000")
    assert "Неверный код" in bot.last_text(ADMIN.id)
    assert (await deals(sessionmaker))[0].status is DealStatus.DISPUTED  # nothing happened
    await bot.send(ADMIN, code(secret, clock))
    assert "Решение по сделке" in bot.last_text(ADMIN.id)

    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.PAYOUT_PENDING
    split = await payouts(sessionmaker, deal.id)
    assert split[PayoutKind.SPLIT_BUYER].amount == 70_000_000
    assert split[PayoutKind.SPLIT_SELLER].amount == 30_000_000
    await drain(ctx, bot)
    assert "Спор решён" in bot.last_text(ALICE.id)
    assert "делим 70/30" in bot.last_text(BOB.id)


async def test_totp_code_is_single_use(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    clock: Clock,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.DISPUTE_NO_REASON, id=deal.id, v=deal.version).pack())
    secret = await make_admin(bot, sessionmaker, clock)
    await bot.press(ADMIN, AdmCb(a=AdmAct.CONFIRM, id=deal.id, x="buyer.0").pack())
    await bot.send(ADMIN, "Продавец не выполнил обязательства")
    used = code(secret, clock)
    await bot.send(ADMIN, used)
    assert "Решение по сделке" in bot.last_text(ADMIN.id)
    # the same code again (e.g. an intercepted one) is refused even for another action
    await bot.press(ADMIN, AdmCb(a=AdmAct.CONFIRM, id=deal.id, x="seller.0").pack())
    await bot.send(ADMIN, "повтор")
    await bot.send(ADMIN, used)
    assert "уже использован" in bot.last_text(ADMIN.id)


async def test_approve_large_payout_from_panel(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    clock: Clock,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    async with sessionmaker() as s, s.begin():
        for user in (ALICE, BOB):
            s.add(User(id=user.id, username=user.username, first_name=user.first_name, deals_completed=5))
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 2500 usdt cb Разработка сайта")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    async with ctx.uow() as uow:
        await uow.deals.accept_deal(BOB.id, deal.id, version=1)
        await uow.deals.start_payment(ALICE.id, deal.id)
    [invoice_id] = list(fake.invoices)
    fake.pay(invoice_id)
    async with ctx.uow() as uow:
        await uow.deals.refresh_payment(ALICE.id, deal.id)
        await uow.deals.seller_confirm(BOB.id, deal.id)
        await uow.deals.buyer_confirm(ALICE.id, deal.id)
    [payout] = (await payouts(sessionmaker, deal.id)).values()
    assert payout.status is PayoutStatus.AWAITING_APPROVAL

    secret = await make_admin(bot, sessionmaker, clock)
    await bot.send(ADMIN, "/admin")
    await press(bot, ADMIN, "На подтверждении")
    await press(bot, ADMIN, f"#{payout.id}")
    assert "2550.00 USDT" not in bot.last_text(ADMIN.id)  # the seller's amount, not the buyer's
    assert "2500.00 USDT" in bot.last_text(ADMIN.id)
    await press(bot, ADMIN, "Подтвердить")
    await bot.send(ADMIN, code(secret, clock))
    [payout] = (await payouts(sessionmaker, deal.id)).values()
    assert payout.status is PayoutStatus.QUEUED


async def test_assign_unidentified_payment_from_panel(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    clock: Clock,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 100 usdt cb Логотип")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    async with ctx.uow() as uow:
        await uow.deals.accept_deal(BOB.id, deal.id, version=1)
        await uow.deals.start_payment(ALICE.id, deal.id)
    [invoice_id] = list(fake.invoices)
    paid = fake.pay(invoice_id)
    paid["payload"] = "something-else"  # not our payload → not credited automatically
    async with ctx.uow() as uow:
        await uow.deals.refresh_payment(ALICE.id, deal.id)
    assert (await deals(sessionmaker))[0].status is DealStatus.AWAITING_PAYMENT

    secret = await make_admin(bot, sessionmaker, clock)
    await bot.send(ADMIN, "/admin")
    assert "Неопознанные платежи: <b>1</b>" in bot.last_text(ADMIN.id)
    await press(bot, ADMIN, "Неопознанные")
    await press(bot, ADMIN, "#")
    card = bot.last_text(ADMIN.id)
    assert "102.00 USDT" in card
    assert deal.code in card  # suggested as a likely match
    await press(bot, ADMIN, "Привязать к сделке")
    await bot.send(ADMIN, deal.code)
    await bot.send(ADMIN, code(secret, clock))
    assert "Платёж привязан к сделке" in bot.last_text(ADMIN.id)
    assert (await deals(sessionmaker))[0].status is DealStatus.FUNDED


async def test_admin_without_totp_cannot_move_money(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.DISPUTE_NO_REASON, id=deal.id, v=deal.version).pack())
    await bot.send(ADMIN, "/start")
    async with sessionmaker() as s, s.begin():
        await s.execute(sa.update(User).where(User.id == ADMIN.id).values(role=UserRole.ADMIN))
    await bot.press(ADMIN, AdmCb(a=AdmAct.CONFIRM, id=deal.id, x="seller.1").pack())
    await bot.send(ADMIN, "Продавец всё выполнил")
    assert "Сначала подключите TOTP" in bot.last_text(ADMIN.id)
    async with sessionmaker() as s:
        assert (await s.get(Deal, deal.id)).status is DealStatus.DISPUTED  # type: ignore[union-attr]
