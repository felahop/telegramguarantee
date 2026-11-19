"""End-to-end bot flows: real handlers, real database, Telegram API recorded."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from aiogram.methods import AnswerInlineQuery, SendMessage
from aiogram.types import Chat, InlineQueryResultArticle, Message, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.callbacks import DealAct, DealCb, MenuAct, MenuCb
from app.bot.context import BotContext
from app.db.models import Deal, NotificationOutbox, SavedAddress, User
from app.domain.enums import DealSource, DealStatus, FeePayer, Network, PartyRole
from tests import addresses
from tests.bot.conftest import ALICE, BOB, EVE
from tests.bot.harness import BotHarness, TgUser


async def deals(sm: async_sessionmaker[AsyncSession]) -> list[Deal]:
    async with sm() as s:
        return list((await s.execute(sa.select(Deal).order_by(Deal.id))).scalars())


async def outbox(sm: async_sessionmaker[AsyncSession], user_id: int) -> list[str]:
    async with sm() as s:
        rows = await s.execute(
            sa.select(NotificationOutbox.kind).where(NotificationOutbox.user_id == user_id)
        )
        return list(rows.scalars())


async def press(bot: BotHarness, user: TgUser, button: str) -> None:
    shown = bot.last_for(user.id)
    assert shown is not None, "nothing on screen"
    await bot.press(user, shown.data(button), message_id=shown.message_id)


async def wizard_to_counterparty(
    bot: BotHarness,
    user: TgUser,
    *,
    role: str = "Я покупатель",
    network: str = "CryptoBot",
    amount: str = "150",
) -> None:
    await bot.send(user, "/start")
    await press(bot, user, "Новая сделка")
    await press(bot, user, role)
    await press(bot, user, network)
    await bot.send(user, amount)
    await bot.send(user, "Логотип для кофейни, 3 правки")
    await press(bot, user, "24 часа")
    await press(bot, user, "Покупатель")


# ── start and menu ───────────────────────────────────────────────────────────


async def test_start_registers_user(bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await bot.send(ALICE, "/start")
    assert "Гарант P2P-сделок" in bot.last_text(ALICE.id)
    async with sessionmaker() as s:
        user = await s.get(User, ALICE.id)
        assert user is not None
        assert user.username == "alice_buyer"
        assert user.started_at is not None


async def test_duplicate_update_is_processed_once(bot: BotHarness) -> None:
    update = Update(
        update_id=777,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=ALICE.id, type="private"),
            from_user=ALICE.as_tg(),
            text="/start",
        ),
    )
    await bot.dp.feed_update(bot.bot, update)
    await bot.dp.feed_update(bot.bot, update)
    assert sum(isinstance(c, SendMessage) for c in bot.session.calls) == 1


async def test_rate_limit(ctx: BotContext) -> None:
    limited = BotHarness.create(ctx, rate_limit=3)
    for _ in range(6):
        await limited.send(ALICE, "/menu")
    texts = [s.text for s in limited.shown(ALICE.id)]
    assert texts.count("Главное меню") == 3
    assert texts.count("Слишком много действий. Подождите немного.") == 1


async def test_banned_user_is_stopped(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(EVE, "/start")
    async with sessionmaker() as s, s.begin():
        await s.execute(sa.update(User).where(User.id == EVE.id).values(is_banned=True))
    await bot.send(EVE, "/new")
    assert "заблокирован" in bot.last_text(EVE.id)


# ── wizard ───────────────────────────────────────────────────────────────────


async def test_wizard_creates_deal_with_known_counterparty(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await wizard_to_counterparty(bot, ALICE)
    assert "С кем сделка" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "@bob_seller")
    preview = bot.last_text(ALICE.id)
    assert "Проверьте сделку" in preview
    assert "К оплате: <b>153.00 USDT</b>" in preview
    assert "Вторая сторона: @bob_seller" in preview
    await press(bot, ALICE, "Создать сделку")
    assert "Сделка создана и отправлена второй стороне" in bot.last_text(ALICE.id)

    [deal] = await deals(sessionmaker)
    assert (deal.buyer_id, deal.invited_user_id, deal.amount, deal.buyer_pays) == (
        ALICE.id,
        BOB.id,
        150_000_000,
        153_000_000,
    )
    assert (deal.network, deal.fee_payer, deal.execution_hours, deal.source) == (
        Network.CRYPTOBOT,
        FeePayer.BUYER,
        24,
        DealSource.WIZARD,
    )
    assert "deal_offer" in await outbox(sessionmaker, BOB.id)


async def test_wizard_back_and_cancel(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    await press(bot, ALICE, "Новая сделка")
    await press(bot, ALICE, "Я покупатель")
    await press(bot, ALICE, "CryptoBot")
    assert "Шаг 3" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "Назад")
    assert "Шаг 2" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "Отмена")
    assert "отменено" in bot.last_text(ALICE.id)


async def test_wizard_uses_one_message(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    await press(bot, ALICE, "Новая сделка")
    first = bot.last_for(ALICE.id)
    await press(bot, ALICE, "Я продавец")
    await press(bot, ALICE, "CryptoBot")
    assert first is not None
    assert bot.last_for(ALICE.id).message_id == first.message_id  # type: ignore[union-attr]


async def test_wizard_amount_limit_and_validation(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    await press(bot, ALICE, "Новая сделка")
    await press(bot, ALICE, "Я покупатель")
    await press(bot, ALICE, "CryptoBot")
    await bot.send(ALICE, "1000")
    assert "Максимальная сумма для вашего аккаунта — 300 USDT" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "сто")
    assert "Неверная сумма" in bot.last_text(ALICE.id) or "Шаг 3" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "1")
    assert "Минимальная сумма" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "50")
    assert "Шаг 4" in bot.last_text(ALICE.id)


async def test_wizard_onchain_seller_address(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession], ctx: BotContext
) -> None:
    await wizard_to_counterparty(bot, ALICE, role="Я продавец", network="TRC-20", amount="50")
    assert "Адрес для выплаты" in bot.last_text(ALICE.id)
    await bot.send(ALICE, "TNotAValidAddress")
    assert "Неверный адрес" in bot.last_text(ALICE.id)
    own = ctx.chains.networks[Network.TRON].deposit_address
    await bot.send(ALICE, own)
    assert "адрес самого бота" in bot.last_text(ALICE.id)
    good = addresses.tron(55)
    await bot.send(ALICE, good)
    assert "С кем сделка" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "ссылку-приглашение")
    assert "Адрес выплаты: <code>" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "Создать сделку")
    assert "Отправьте приглашение" in bot.last_text(ALICE.id)
    [deal] = await deals(sessionmaker)
    assert (deal.seller_id, deal.seller_payout_address, deal.network) == (ALICE.id, good, Network.TRON)
    assert deal.seller_receives == 50_000_000 - 1_500_000  # buyer pays the fee, seller the network fee
    async with sessionmaker() as s:
        saved = (await s.execute(sa.select(SavedAddress))).scalar_one()
        assert saved.address == good


async def test_contact_picker(bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await wizard_to_counterparty(bot, ALICE)
    await press(bot, ALICE, "Выбрать из контактов")
    assert "Нажмите кнопку ниже" in bot.last_text(ALICE.id)
    await bot.share_user(ALICE, BOB)  # Bob never opened the bot: still bound by user_id
    assert "Вторая сторона: @bob_seller" in bot.last_text(ALICE.id)
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    assert deal.invited_user_id == BOB.id


# ── one-line commands ────────────────────────────────────────────────────────


async def test_buy_command_goes_to_preview(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 120 USDT trc20 Логотип для кофейни")
    preview = bot.last_text(ALICE.id)
    assert "Проверьте сделку" in preview
    assert "USDT · TRC-20" in preview
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    assert (deal.buyer_id, deal.invited_user_id, deal.network, deal.amount, deal.source) == (
        ALICE.id,
        BOB.id,
        Network.TRON,
        120_000_000,
        DealSource.COMMAND,
    )


async def test_deal_command_asks_role(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/deal 10 usdt cb тест")
    assert "Кто вы в этой сделке" in bot.last_text(ALICE.id)


@pytest.mark.parametrize(
    ("command", "answer"),
    [
        ("/sell 150 RUB подписка", "только в USDT"),
        ("/sell @alice_buyer 10 usdt x y z", "самим собой"),
        ("/buy @x 10 usdt тест", "Неверный @username"),
    ],
)
async def test_command_errors(bot: BotHarness, command: str, answer: str) -> None:
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, command)
    assert answer in bot.last_text(ALICE.id)


# ── invites and acceptance ───────────────────────────────────────────────────


async def _create_open_deal(bot: BotHarness, user: TgUser) -> None:
    await wizard_to_counterparty(bot, user)
    await press(bot, user, "ссылку-приглашение")
    await press(bot, user, "Создать сделку")


async def test_open_invite_needs_creator_confirmation(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _create_open_deal(bot, ALICE)
    [deal] = await deals(sessionmaker)
    await bot.send(BOB, f"/start j_{deal.invite_token}")
    assert "Вам предлагают стать стороной: продавец" in bot.last_text(BOB.id)
    await press(bot, BOB, "Принять условия")
    assert "Создатель сделки подтвердит" in bot.last_text(BOB.id)
    assert "confirm_counterparty" in await outbox(sessionmaker, ALICE.id)

    await bot.send(EVE, f"/start j_{deal.invite_token}")
    assert "другому пользователю или уже занята" in bot.last_text(EVE.id)

    await bot.press(ALICE, DealCb(a=DealAct.CONFIRM_CANDIDATE, id=deal.id, v=deal.version).pack())
    [deal] = await deals(sessionmaker)
    assert (deal.status, deal.seller_id) == (DealStatus.ACCEPTED, BOB.id)


async def test_invite_for_someone_else(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await wizard_to_counterparty(bot, ALICE)
    await bot.send(ALICE, "@bob_seller")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    await bot.send(EVE, f"/start j_{deal.invite_token}")
    assert "другому пользователю" in bot.last_text(EVE.id)
    await bot.press(EVE, DealCb(a=DealAct.ACCEPT, id=deal.id, v=1).pack())
    assert any("другому пользователю" in a for a in bot.alerts())


async def test_onchain_accept_asks_for_payout_address(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 40 usdt bep20 Верстка лендинга")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    await bot.send(BOB, f"/start j_{deal.invite_token}")
    await press(bot, BOB, "Принять условия")
    assert "Адрес для выплаты" in bot.last_text(BOB.id)
    await bot.send(BOB, "0x123")
    assert "Неверный адрес" in bot.last_text(BOB.id)
    await bot.send(BOB, addresses.evm(77).lower())
    assert "Вы приняли условия" in bot.last_text(BOB.id)
    [deal] = await deals(sessionmaker)
    assert (deal.status, deal.seller_payout_address) == (DealStatus.ACCEPTED, addresses.evm(77))


async def test_stale_card_is_rejected(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await bot.send(BOB, "/start")
    await bot.send(ALICE, "/start")
    await bot.send(ALICE, "/buy @bob_seller 40 usdt cb Верстка")
    await press(bot, ALICE, "Создать сделку")
    [deal] = await deals(sessionmaker)
    stale = DealCb(a=DealAct.ACCEPT, id=deal.id, v=deal.version).pack()

    await bot.press(ALICE, DealCb(a=DealAct.EDIT, id=deal.id, v=deal.version).pack())
    await press(bot, ALICE, "Изменить")
    await press(bot, ALICE, "Сумма")
    await bot.send(ALICE, "45")
    await press(bot, ALICE, "Сохранить изменения")
    [deal] = await deals(sessionmaker)
    assert (deal.version, deal.amount) == (2, 45_000_000)

    await bot.press(BOB, stale)
    assert any("Условия сделки изменились" in a for a in bot.alerts())
    await bot.press(BOB, DealCb(a=DealAct.ACCEPT, id=deal.id, v=2).pack())
    [deal] = await deals(sessionmaker)
    assert deal.status is DealStatus.ACCEPTED


async def test_cancel_needs_confirmation(
    bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _create_open_deal(bot, ALICE)
    [deal] = await deals(sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.CANCEL, id=deal.id, v=deal.version).pack())
    assert "Отменить сделку?" in bot.last_text(ALICE.id)
    assert (await deals(sessionmaker))[0].status is DealStatus.CREATED
    await press(bot, ALICE, "Да, отменить")
    assert (await deals(sessionmaker))[0].status is DealStatus.CANCELLED


async def test_template_and_repeat(bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _create_open_deal(bot, ALICE)
    [deal] = await deals(sessionmaker)
    await bot.press(ALICE, DealCb(a=DealAct.CANCEL_CONFIRM, id=deal.id, v=deal.version).pack())
    await press(bot, ALICE, "В шаблоны")
    assert any("Шаблон" in a for a in bot.alerts())
    await bot.press(ALICE, MenuCb(a=MenuAct.TEMPLATES).pack())
    await press(bot, ALICE, "150 USDT")
    assert "С кем сделка" in bot.last_text(ALICE.id)  # everything else came from the template

    await bot.press(ALICE, DealCb(a=DealAct.REPEAT, id=deal.id, v=deal.version).pack())
    assert "С кем сделка" in bot.last_text(ALICE.id)  # open-link deal: counterparty asked again


async def test_my_deals_list(bot: BotHarness) -> None:
    await _create_open_deal(bot, ALICE)
    await bot.press(ALICE, MenuCb(a=MenuAct.DEALS).pack())
    shown = bot.last_for(ALICE.id)
    assert shown is not None
    assert "Ваши сделки" in shown.text
    assert any("150.00 USDT" in text for text, _ in shown.buttons())


# ── inline mode ──────────────────────────────────────────────────────────────


async def test_inline_requires_started_bot(bot: BotHarness) -> None:
    calls = await bot.inline(EVE, "100 usdt логотип")
    [answer] = [c for c in calls if isinstance(c, AnswerInlineQuery)]
    assert answer.results == []
    assert answer.button is not None
    assert answer.button.start_parameter == "inline"


async def test_inline_deal_flow(bot: BotHarness, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await bot.send(ALICE, "/start")
    await bot.send(BOB, "/start")
    calls = await bot.inline(ALICE, "@bob_seller 100 usdt cb Логотип для кофейни")
    [answer] = [c for c in calls if isinstance(c, AnswerInlineQuery)]
    results = [r for r in answer.results if isinstance(r, InlineQueryResultArticle)]
    assert len(results) == 2
    buy = next(r for r in results if r.id.startswith("buyer:"))
    assert "Сделка через гаранта" in buy.input_message_content.message_text  # type: ignore[union-attr]
    url = buy.reply_markup.inline_keyboard[0][0].url  # type: ignore[union-attr]
    assert url is not None
    token = url.split("start=i_", 1)[1]

    await bot.send(ALICE, f"/start i_{token}")  # the creator opening their own card creates nothing
    assert await deals(sessionmaker) == []

    await bot.send(BOB, f"/start i_{token}")
    [deal] = await deals(sessionmaker)
    assert (deal.creator_id, deal.creator_role, deal.invited_user_id, deal.source) == (
        ALICE.id,
        PartyRole.BUYER,
        BOB.id,
        DealSource.INLINE,
    )
    assert "Принять условия" in str(bot.last_for(BOB.id).buttons())  # type: ignore[union-attr]

    await bot.send(EVE, f"/start i_{token}")
    assert "другому пользователю" in bot.last_text(EVE.id)
    assert len(await deals(sessionmaker)) == 1  # the same link never creates a second deal


async def test_inline_onchain_seller_needs_saved_address(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    calls = await bot.inline(ALICE, "100 usdt trc20 Логотип")
    [answer] = [c for c in calls if isinstance(c, AnswerInlineQuery)]
    ids = [r.id for r in answer.results]
    assert "need-address" in ids
    assert any(i.startswith("buyer:") for i in ids)


async def test_inline_bad_query_shows_hint(bot: BotHarness) -> None:
    await bot.send(ALICE, "/start")
    calls = await bot.inline(ALICE, "150 RUB что-то")
    [answer] = [c for c in calls if isinstance(c, AnswerInlineQuery)]
    assert answer.results[0].id == "error"
