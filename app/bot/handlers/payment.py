"""Buyer payment: create the payment request (CryptoBot invoice or on-chain instructions), re-check a
payment on demand; the seller may accept an underpayment."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts as T
from app.bot.callbacks import DealAct, DealCb
from app.bot.context import BotContext
from app.bot.views import deal_view
from app.db.models import User
from app.domain.enums import DealStatus
from app.domain.errors import DomainError, Forbidden
from app.services.tx_claims import submit_claim


class TxClaimSG(StatesGroup):
    tx_hash = State()


async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]


async def on_pay(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, callback_data.id)
        if deal.buyer_id != user.id:
            raise Forbidden("only the buyer pays")
        if deal.status is DealStatus.ACCEPTED:
            await uow.deals.start_payment(user.id, deal.id)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer()
    await _edit(callback, text, markup)


async def on_check_payment(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.refresh_payment(user.id, callback_data.id)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    if deal.status is DealStatus.AWAITING_PAYMENT:
        await callback.answer(
            "Оплата пока не поступила. Если вы только что оплатили — подождите минуту.", show_alert=True
        )
    else:
        await callback.answer("Оплата получена ✅")
    await _edit(callback, text, markup)


async def on_accept_underpayment(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.accept_underpayment(user.id, callback_data.id)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer("Недоплата принята — сделка оплачена.")
    await _edit(callback, text, markup)


async def on_tx_claim(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, callback_data.id)
    if deal.buyer_id != user.id:
        raise Forbidden("only the buyer sends the transaction")
    await state.set_state(TxClaimSG.tx_hash)
    await state.update_data(claim_deal_id=deal.id)
    await callback.answer()
    await callback.bot.send_message(  # type: ignore[union-attr]
        user.id,
        "🧾 Пришлите hash (ID) транзакции, которой вы оплатили сделку. Бот найдёт её в сети и зачислит, "
        "если перевод USDT пришёл на адрес гаранта. Если сумма не совпала, перевод проверит администратор.",
    )


async def on_tx_claim_text(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    deal_id = int((await state.get_data())["claim_deal_id"])
    try:
        async with ctx.uow() as uow:
            await submit_claim(
                uow.session, user_id=user.id, deal_id=deal_id, tx_hash=message.text or "", now=ctx.clock()
            )
    except DomainError as exc:
        await message.answer(f"⚠️ {T.error_text(exc)}")
        return
    await state.clear()
    await message.answer(
        "✅ Принято. Проверяем транзакцию в сети — результат придёт сюда в течение пары минут."
    )


def build_router() -> Router:
    router = Router(name="payment")
    router.callback_query.register(on_pay, DealCb.filter(F.a == DealAct.PAY))
    router.callback_query.register(on_check_payment, DealCb.filter(F.a == DealAct.CHECK_PAYMENT))
    router.callback_query.register(on_accept_underpayment, DealCb.filter(F.a == DealAct.ACCEPT_UNDERPAYMENT))
    router.callback_query.register(on_tx_claim, DealCb.filter(F.a == DealAct.TX_CLAIM))
    router.message.register(on_tx_claim_text, StateFilter(TxClaimSG.tx_hash), F.text)
    return router
