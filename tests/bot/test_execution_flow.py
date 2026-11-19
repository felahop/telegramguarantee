"""After payment, through the bot's buttons: seller done → buyer received (with the irreversible
confirmation) → payout → completed; mutual cancel; refund after deadline; disputes; refund address;
deal chat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from aiogram.methods import CopyMessage
from aiogram.types import Chat, Message, PhotoSize, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.callbacks import DealAct, DealCb
from app.bot.context import BotContext
from app.db.models import Deal, DealMessage, Dispute, Payout, User
from app.domain.enums import DealStatus, DisputeStatus, Network, PayoutKind, PayoutStatus, UserRole
from app.domain.money import asset_code
from app.providers.cryptobot.client import CryptoPayClient
from app.services import ledger as L
from app.services.deals import DealService
from app.services.ledger import Ledger
from app.worker.cryptobot import CryptoBotPayouts
from tests import addresses
from tests.bot.conftest import ALICE, BOB
from tests.bot.harness import BotHarness
from tests.bot.test_bot_flows import deals, outbox, press
from tests.bot.test_notifications import drain
from tests.cryptobot_fake import FakeCryptoPay
from tests.integration.deal_world import Clock, FakeGateway


async def funded(
    bot: BotHarness, ctx: BotContext, fake: FakeCryptoPay, sm: async_sessionmaker[AsyncSession]
) -> Deal:
    """Alice buys from Bob for 100 USDT via CryptoBot; returns the funded deal."""
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 100 usdt cb Логотип для кофейни")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sm)
    await bot.send(BOB, f"/start j_{deal.invite_token}")
    await press(bot, BOB, "Принять условия")
    await bot.press(ALICE, DealCb(a=DealAct.PAY, id=deal.id, v=deal.version).pack())
    [invoice_id] = list(fake.invoices)
    fake.pay(invoice_id)
    await press(bot, ALICE, "проверить")
    await drain(ctx, bot)
    [deal] = await deals(sm)
    assert deal.status is DealStatus.FUNDED
    return deal


async def payouts(sm: async_sessionmaker[AsyncSession], deal_id: int) -> list[Payout]:
    async with sm() as s:
        return list((await s.execute(sa.select(Payout).where(Payout.deal_id == deal_id))).scalars())


async def test_full_deal_through_buttons(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    cryptopay: CryptoPayClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    assert "Оплата получена — деньги у гаранта" in bot.last_text(BOB.id)

    await press(bot, BOB, "Я выполнил обязательства")
    await drain(ctx, bot)
    notice = bot.last_text(ALICE.id)
    assert "Продавец сообщает, что выполнил обязательства" in notice

    await press(bot, ALICE, "Получил, всё верно")
    screen = bot.last_text(ALICE.id)
    assert "отменить будет нельзя" in screen
    assert "100.00 USDT" in screen
    assert (await deals(sessionmaker))[0].status is DealStatus.SELLER_CONFIRMED  # nothing happened yet

    await press(bot, ALICE, "Да, всё получил")
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.PAYOUT_PENDING

    assert await CryptoBotPayouts(ctx, cryptopay).run_once()
    [transfer] = fake.calls_to("transfer")
    assert (transfer["user_id"], transfer["amount"]) == (BOB.id, "100")
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.COMPLETED
    await drain(ctx, bot)
    assert "Сделка завершена" in bot.last_text(ALICE.id)
    assert "Сделка завершена" in bot.last_text(BOB.id)


async def test_confirmation_needs_the_fresh_screen(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await press(bot, BOB, "Я выполнил обязательства")
    await drain(ctx, bot)
    # a forged / stale confirmation (token not issued for this screen) does nothing
    await bot.press(ALICE, DealCb(a=DealAct.BUYER_GOT_OK, id=deal.id, v=12345).pack())
    assert any("Подтверждение устарело" in a for a in bot.alerts())
    # the seller cannot open the buyer's confirmation at all
    await bot.press(BOB, DealCb(a=DealAct.BUYER_GOT, id=deal.id, v=deal.version).pack())
    assert any("недоступно" in a for a in bot.alerts())
    # a token works exactly once
    await press(bot, ALICE, "Получил, всё верно")
    confirm = bot.last_for(ALICE.id).data("Да, всё получил")  # type: ignore[union-attr]
    await bot.press(ALICE, confirm)
    await bot.press(ALICE, confirm)
    assert len(await payouts(sessionmaker, deal.id)) == 1


async def test_buyer_cannot_confirm_before_seller(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    labels = [text for text, _ in bot.last_for(ALICE.id).buttons()]  # type: ignore[union-attr]
    assert not any("Получил" in label for label in labels)
    await bot.press(ALICE, DealCb(a=DealAct.BUYER_GOT, id=deal.id, v=deal.version).pack())
    assert any("уже выполнено или недоступно" in a for a in bot.alerts())


async def test_mutual_cancel_through_buttons(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.REQ_CANCEL, id=deal.id, v=deal.version).pack())
    assert "Вы предложили отменить сделку" in bot.last_text(ALICE.id)
    await drain(ctx, bot)
    assert "предлагает отменить сделку" in bot.last_text(BOB.id)
    await press(bot, BOB, "Согласиться на отмену")
    assert "деньги вернутся покупателю" in bot.last_text(BOB.id)
    await press(bot, BOB, "Да, отменить сделку")
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.REFUND_PENDING
    [refund] = await payouts(sessionmaker, deal.id)
    assert (refund.kind, refund.recipient_user_id, refund.amount) == (
        PayoutKind.BUYER_REFUND,
        ALICE.id,
        102_000_000,
    )


async def test_refund_request_and_objection(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    clock: Clock,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    assert "Запросить возврат" not in str(bot.last_for(ALICE.id).buttons())  # type: ignore[union-attr]
    clock.advance(timedelta(hours=25))
    await bot.press(ALICE, DealCb(a=DealAct.VIEW, id=deal.id, v=deal.version).pack())
    await press(bot, ALICE, "Запросить возврат")
    await drain(ctx, bot)
    assert "Покупатель просит возврат" in bot.last_text(BOB.id)
    await press(bot, BOB, "Возразить")
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.DISPUTED


async def test_dispute_with_reason_and_withdrawal(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    async with sessionmaker() as s, s.begin():
        s.add(User(id=9999, username="arbiter", first_name="Arbiter", role=UserRole.ADMIN))
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.DISPUTE, id=deal.id, v=deal.version).pack())
    assert "Опишите одним сообщением" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "Продавец не выходит на связь третий день")
    assert "Спор открыт" in bot.last_text(ALICE.id)
    async with sessionmaker() as s:
        dispute = (await s.execute(sa.select(Dispute))).scalar_one()
        assert dispute.reason_text == "Продавец не выходит на связь третий день"
    assert "admin_dispute_opened" in await outbox(sessionmaker, 9999)
    assert "Отозвать спор" not in str(bot.last_for(BOB.id).buttons() if bot.last_for(BOB.id) else "")
    await press(bot, ALICE, "Отозвать спор")
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.FUNDED
    async with sessionmaker() as s:
        assert (await s.execute(sa.select(Dispute))).scalar_one().status is DisputeStatus.WITHDRAWN


async def test_onchain_refund_address_from_the_card(
    bot: BotHarness, ctx: BotContext, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 50 usdt trc20 Перевод текста")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    async with sessionmaker() as session, session.begin():
        # Bob accepts with an address and the on-chain payment arrives (the TRON watcher is a later
        # module: the deposit is posted directly), then both agree to cancel.
        svc = DealService(
            session, ctx.settings, ctx.chains, gateway=FakeGateway(ctx.settings, ctx.chains), clock=ctx.clock
        )
        await svc.accept_deal(BOB.id, deal.id, version=1, payout_address=addresses.tron(3))
        request = await svc.start_payment(ALICE.id, deal.id)
        await Ledger(session).post(
            idempotency_key="dep",
            kind="deposit",
            postings=L.deposit_matched(
                asset=asset_code("USDT", Network.TRON),
                wallet="hot",
                deal_id=deal.id,
                amount=request.expected_amount,
            ),
        )
        await svc.fund(deal.id)
        await svc.request_cancel(ALICE.id, deal.id)
        await svc.request_cancel(BOB.id, deal.id)
    await bot.press(ALICE, DealCb(a=DealAct.VIEW, id=deal.id, v=deal.version).pack())
    assert "Укажите адрес для возврата" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "Указать адрес возврата")
    await bot.send(ALICE, "TBadAddress")
    assert "Неверный адрес" in bot.last_text(ALICE.id)
    await bot.send(ALICE, addresses.tron(4))
    assert "Адрес принят" in bot.last_text(ALICE.id)
    [refund] = await payouts(sessionmaker, deal.id)
    assert (refund.status, refund.recipient_address) == (PayoutStatus.QUEUED, addresses.tron(4))


async def test_deal_chat_relays_and_flags_requisites(
    paying_bot: BotHarness,
    ctx: BotContext,
    fake: FakeCryptoPay,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bot = paying_bot
    deal = await funded(bot, ctx, fake, sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.CHAT, id=deal.id, v=deal.version).pack())
    assert "Чат по сделке" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "Можно доплатить на карту 4111 1111 1111 1111?")
    relayed = bot.last_for(BOB.id)
    assert relayed is not None
    assert "сообщение от покупателя" in relayed.text
    assert "4111 1111 1111 1111" in relayed.text
    assert "Не платите и не переходите мимо бота" in relayed.text
    async with sessionmaker() as s:
        [stored] = list((await s.execute(sa.select(DealMessage))).scalars())
        assert (stored.sender_id, stored.flagged) == (ALICE.id, ["card"])

    await press(bot, BOB, "Ответить")
    await bot.send(BOB, "Нет, только через бота. Отправляю макет.")
    assert "сообщение от продавца" in bot.last_text(ALICE.id)
    assert "мимо бота" not in bot.last_text(ALICE.id)

    photo = Message(
        message_id=500,
        date=datetime.now(UTC),
        chat=Chat(id=BOB.id, type="private"),
        from_user=BOB.as_tg(),
        caption="макет v1",
        photo=[PhotoSize(file_id="PHOTO1", file_unique_id="u1", width=10, height=10)],
    )
    await bot.dp.feed_update(bot.bot, Update(update_id=10_000, message=photo))
    [copy] = [c for c in bot.session.calls if isinstance(c, CopyMessage)]
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (ALICE.id, BOB.id, 500)
    async with sessionmaker() as s:
        files = [m.file_id for m in (await s.execute(sa.select(DealMessage))).scalars()]
        assert "PHOTO1" in files

    await bot.send(BOB, "/done")
    assert "Чат закрыт" in bot.last_text(BOB.id)
    await bot.send(BOB, "это уже не в чат")
    assert "Не понял сообщение" in bot.last_text(BOB.id)
