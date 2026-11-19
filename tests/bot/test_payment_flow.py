"""Buyer pays a CryptoBot deal from the bot: invoice button, manual check, notification to the seller."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.callbacks import DealAct, DealCb
from app.bot.context import BotContext
from app.domain.enums import DealStatus
from tests.bot.conftest import ALICE, BOB
from tests.bot.harness import BotHarness
from tests.bot.test_bot_flows import deals, press
from tests.bot.test_notifications import drain
from tests.cryptobot_fake import FakeCryptoPay


async def test_buyer_pays_from_the_card(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 100 usdt cb Логотип для кофейни")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    await bot.send(BOB, f"/start j_{deal.invite_token}")
    await press(bot, BOB, "Принять условия")
    await drain(ctx, bot)

    # Alice got "accepted" with the live card → "Перейти к оплате"
    await press(bot, ALICE, "Перейти к оплате")
    card = bot.last_for(ALICE.id)
    assert card is not None
    assert "Оплатите <b>102.00 USDT</b>" in card.text
    [invoice_id] = list(fake.invoices)
    assert card.data("Оплатить 102.00 USDT") == f"https://t.me/CryptoTestnetBot?start=IV{invoice_id}"

    await press(bot, ALICE, "проверить")
    assert any("пока не поступила" in a for a in bot.alerts())

    fake.pay(invoice_id)
    await press(bot, ALICE, "проверить")
    assert any("Оплата получена" in a for a in bot.alerts())
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.FUNDED
    assert "Оплачено, деньги у гаранта" in bot.last_text(ALICE.id)

    await drain(ctx, bot)
    assert "Оплата получена — деньги у гаранта" in bot.last_text(BOB.id)


async def test_only_buyer_can_start_payment(
    paying_bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    bot = paying_bot
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 100 usdt cb Логотип")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    await bot.send(BOB, f"/start j_{deal.invite_token}")
    await press(bot, BOB, "Принять условия")
    await bot.press(BOB, DealCb(a=DealAct.PAY, id=deal.id, v=deal.version).pack())
    assert any("недоступно" in a for a in bot.alerts())
    async with sessionmaker() as s:
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(sa.text("payment_requests")))
        ).scalar_one() == 0
