"""Buyer payment: create the payment request (CryptoBot invoice or on-chain instructions), re-check a
payment on demand; the seller may accept an underpayment."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import DealAct, DealCb
from app.bot.context import BotContext
from app.bot.views import deal_view
from app.db.models import User
from app.domain.enums import DealStatus
from app.domain.errors import Forbidden


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


def build_router() -> Router:
    router = Router(name="payment")
    router.callback_query.register(on_pay, DealCb.filter(F.a == DealAct.PAY))
    router.callback_query.register(on_check_payment, DealCb.filter(F.a == DealAct.CHECK_PAYMENT))
    router.callback_query.register(on_accept_underpayment, DealCb.filter(F.a == DealAct.ACCEPT_UNDERPAYMENT))
    return router
