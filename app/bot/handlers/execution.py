"""Actions after the deal is paid: seller done, buyer received (irreversible, confirmed), mutual
cancel, refund after the deadline, disputes, refund address.

Irreversible steps go through a confirmation screen carrying a one-time token (Redis, 5 min):
the second press must come from that fresh screen — an old message, a forwarded button or a
double tap cannot release money by accident.
"""

from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import DealAct, DealCb, WizAct, WizCb
from app.bot.context import BotContext
from app.bot.render import money
from app.bot.texts import esc
from app.bot.views import deal_view
from app.db.models import Deal, User
from app.domain.enums import DealStatus, PartyRole
from app.domain.errors import DomainError, Forbidden, InvalidTransition
from app.services.queries import get_saved_address, remember_address, saved_addresses

CONFIRM_TTL = 300
DISPUTE_TEXT_LIMIT = 2000


class DisputeSG(StatesGroup):
    reason = State()


class RefundAddressSG(StatesGroup):
    address = State()


# ── helpers ──────────────────────────────────────────────────────────────────


async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]


async def _card(
    callback: CallbackQuery, ctx: BotContext, user: User, deal: Deal, *, headline: str = ""
) -> None:
    async with ctx.uow() as uow:
        fresh = await uow.deals.get_for_user(user.id, deal.id)
        text, markup = await deal_view(ctx, uow.session, fresh, user.id)
    await _edit(callback, f"{headline}\n\n{text}" if headline else text, markup)


def _token_key(action: DealAct, deal_id: int, user_id: int) -> str:
    return f"cf:{action.value}:{deal_id}:{user_id}"


async def _issue_token(ctx: BotContext, action: DealAct, deal: Deal, user: User) -> int:
    token = secrets.randbelow(2**31 - 1) + 1
    await ctx.kv.set(_token_key(action, deal.id, user.id), str(token), CONFIRM_TTL)
    return token


async def _consume_token(ctx: BotContext, action: DealAct, deal_id: int, user: User, token: int) -> None:
    key = _token_key(action, deal_id, user.id)
    stored = await ctx.kv.get(key)
    if stored is None or stored != str(token):
        raise ConfirmationExpired("confirmation token mismatch")
    await ctx.kv.delete(key)


class ConfirmationExpired(DomainError):
    code = "confirmation_expired"


async def _party_deal(ctx: BotContext, user: User, deal_id: int, role: PartyRole | None = None) -> Deal:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, deal_id)
    actual = deal.party_role(user.id)
    if actual is None or (role is not None and actual is not role):
        raise Forbidden("not your action")
    return deal


async def _confirm_screen(
    callback: CallbackQuery,
    ctx: BotContext,
    user: User,
    deal: Deal,
    *,
    action: DealAct,
    warning: str,
    yes: str,
) -> None:
    token = await _issue_token(ctx, action, deal, user)
    async with ctx.uow() as uow:
        text, _ = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer()
    await _edit(callback, f"{text}\n\n{warning}", K.confirm_kb(deal, action, token, yes))


# ── seller / buyer confirmations ─────────────────────────────────────────────


async def on_seller_done(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.seller_confirm(user.id, callback_data.id)
    if deal.status is DealStatus.DISPUTED:
        await callback.answer("Покупатель уже запросил возврат — открыт спор, решит арбитр.", show_alert=True)
    else:
        await callback.answer("Покупатель получил уведомление")
    await _card(callback, ctx, user, deal)


async def on_buyer_got(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    deal = await _party_deal(ctx, user, callback_data.id, PartyRole.BUYER)
    if deal.status is not DealStatus.SELLER_CONFIRMED:
        raise InvalidTransition("the seller has not confirmed yet")
    amount = money(deal.seller_receives, deal.decimals)
    warning = (
        f"❗ <b>Деньги уйдут продавцу ({amount}), отменить будет нельзя.</b>\n"
        "Подтверждайте, только если получили всё по условиям сделки. Не подтверждайте по просьбе продавца "
        "«заранее» — если что-то не так, откройте спор."
    )
    await _confirm_screen(
        callback,
        ctx,
        user,
        deal,
        action=DealAct.BUYER_GOT_OK,
        warning=warning,
        yes="✅ Да, всё получил — выплатить продавцу",
    )


async def on_buyer_got_ok(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    await _consume_token(ctx, DealAct.BUYER_GOT_OK, callback_data.id, user, callback_data.v)
    async with ctx.uow() as uow:
        deal = await uow.deals.buyer_confirm(user.id, callback_data.id)
    await callback.answer("Готово: выплата продавцу отправляется")
    await _card(callback, ctx, user, deal, headline="✅ Вы подтвердили получение. Спасибо!")


# ── cancellation by agreement, refund after the deadline ─────────────────────


async def on_request_cancel(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    deal = await _party_deal(ctx, user, callback_data.id)
    buyer = deal.party_role(user.id) is PartyRole.BUYER
    theirs = deal.cancel_req_seller_at if buyer else deal.cancel_req_buyer_at
    if theirs is not None:  # this press completes the mutual cancel → money goes back: confirm
        await _confirm_screen(
            callback,
            ctx,
            user,
            deal,
            action=DealAct.REQ_CANCEL_OK,
            warning="❗ <b>Сделка будет отменена, деньги вернутся покупателю.</b> Подтвердить?",
            yes="✅ Да, отменить сделку",
        )
        return
    async with ctx.uow() as uow:
        deal = await uow.deals.request_cancel(user.id, deal.id)
    await callback.answer("Предложение отправлено второй стороне")
    await _card(callback, ctx, user, deal)


async def on_request_cancel_ok(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    await _consume_token(ctx, DealAct.REQ_CANCEL_OK, callback_data.id, user, callback_data.v)
    async with ctx.uow() as uow:
        deal = await uow.deals.request_cancel(user.id, callback_data.id)
    await callback.answer("Сделка отменена, деньги возвращаются покупателю")
    await _card(callback, ctx, user, deal)


async def on_revoke_cancel(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.revoke_cancel(user.id, callback_data.id)
    await callback.answer("Предложение отмены отозвано")
    await _card(callback, ctx, user, deal)


async def on_request_refund(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.request_refund(user.id, callback_data.id)
    await callback.answer("Запрос отправлен продавцу")
    await _card(callback, ctx, user, deal)


async def on_agree_refund(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    deal = await _party_deal(ctx, user, callback_data.id, PartyRole.SELLER)
    await _confirm_screen(
        callback,
        ctx,
        user,
        deal,
        action=DealAct.AGREE_REFUND_OK,
        warning="❗ <b>Деньги вернутся покупателю, сделка будет закрыта.</b> Подтвердить?",
        yes="✅ Да, вернуть деньги покупателю",
    )


async def on_agree_refund_ok(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    await _consume_token(ctx, DealAct.AGREE_REFUND_OK, callback_data.id, user, callback_data.v)
    async with ctx.uow() as uow:
        deal = await uow.deals.agree_refund(user.id, callback_data.id)
    await callback.answer("Деньги возвращаются покупателю")
    await _card(callback, ctx, user, deal)


async def on_object_refund(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.object_refund(user.id, callback_data.id)
    await callback.answer("Открыт спор — решит арбитр", show_alert=True)
    await _card(callback, ctx, user, deal)


# ── disputes ─────────────────────────────────────────────────────────────────


async def on_dispute(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    deal = await _party_deal(ctx, user, callback_data.id)
    if deal.status not in (DealStatus.FUNDED, DealStatus.SELLER_CONFIRMED):
        raise InvalidTransition("a dispute can be opened only after payment")
    await state.set_state(DisputeSG.reason)
    await state.update_data(dispute_deal_id=deal.id)
    await callback.answer()
    await _edit(
        callback,
        "⚖️ <b>Открытие спора</b>\n\nОпишите одним сообщением, что пошло не так. Деньги будут заморожены до "
        "решения арбитра; он увидит условия сделки, этот текст и переписку в чате сделки.",
        K.dispute_reason_kb(deal),
    )


async def _open_dispute(ctx: BotContext, user: User, deal_id: int, text: str | None) -> Deal:
    async with ctx.uow() as uow:
        return await uow.deals.open_dispute(user.id, deal_id, text)


async def on_dispute_reason(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    deal_id = (await state.get_data())["dispute_deal_id"]
    await state.clear()
    deal = await _open_dispute(ctx, user, deal_id, (message.text or "")[:DISPUTE_TEXT_LIMIT])
    async with ctx.uow() as uow:
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await message.answer(
        f"⚖️ Спор открыт. Арбитр свяжется со сторонами через бота.\n\n{text}", reply_markup=markup
    )


async def on_dispute_no_reason(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    await state.clear()
    deal = await _open_dispute(ctx, user, callback_data.id, None)
    await callback.answer("Спор открыт")
    await _card(callback, ctx, user, deal)


async def on_withdraw_dispute(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.withdraw_dispute(user.id, callback_data.id)
    await callback.answer("Спор отозван, сделка продолжается")
    await _card(callback, ctx, user, deal)


async def on_leave_state(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    """«Отмена» on the dispute/refund-address prompts: back to the card."""
    await state.clear()
    await callback.answer()
    deal = await _party_deal(ctx, user, callback_data.id)
    await _card(callback, ctx, user, deal)


# ── refund address (on-chain refunds) ────────────────────────────────────────


async def on_refund_address(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    deal = await _party_deal(ctx, user, callback_data.id, PartyRole.BUYER)
    async with ctx.uow() as uow:
        saved = await saved_addresses(uow.session, user.id, deal.network)
    await state.set_state(RefundAddressSG.address)
    await state.update_data(refund_deal_id=deal.id)
    await callback.answer()
    await _edit(
        callback,
        f"📮 Пришлите адрес кошелька в сети <b>{esc(ctx.spec(deal.network).title)}</b> для возврата.\n\n"
        "Не указывайте адрес биржи, если не уверены, что она зачислит перевод без мемо.",
        K.refund_address_kb(deal, saved),
    )


async def _set_refund_address(ctx: BotContext, user: User, deal_id: int, address: str) -> Deal:
    async with ctx.uow() as uow:
        deal = await uow.deals.provide_refund_address(user.id, deal_id, address)
        if deal.buyer_refund_address:
            await remember_address(
                uow.session, user.id, deal.network, deal.buyer_refund_address, now=ctx.clock()
            )
        return deal


async def on_refund_address_text(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    deal_id = (await state.get_data())["refund_deal_id"]
    try:
        deal = await _set_refund_address(ctx, user, deal_id, message.text or "")
    except DomainError as exc:
        await message.answer(f"⚠️ {T.error_text(exc)}")
        return
    await state.clear()
    async with ctx.uow() as uow:
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await message.answer(f"✅ Адрес принят, возврат отправляется.\n\n{text}", reply_markup=markup)


async def on_refund_saved_address(
    callback: CallbackQuery, callback_data: WizCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    deal_id = (await state.get_data())["refund_deal_id"]
    async with ctx.uow() as uow:
        saved = await get_saved_address(uow.session, user.id, int(callback_data.val))
    deal = await _set_refund_address(ctx, user, deal_id, saved.address)
    await state.clear()
    await callback.answer("Адрес принят")
    await _card(callback, ctx, user, deal)


def build_router() -> Router:
    router = Router(name="execution")
    register = router.callback_query.register
    register(on_seller_done, DealCb.filter(F.a == DealAct.SELLER_DONE))
    register(on_buyer_got, DealCb.filter(F.a == DealAct.BUYER_GOT))
    register(on_buyer_got_ok, DealCb.filter(F.a == DealAct.BUYER_GOT_OK))
    register(on_request_cancel, DealCb.filter(F.a == DealAct.REQ_CANCEL))
    register(on_request_cancel_ok, DealCb.filter(F.a == DealAct.REQ_CANCEL_OK))
    register(on_revoke_cancel, DealCb.filter(F.a == DealAct.REVOKE_CANCEL))
    register(on_request_refund, DealCb.filter(F.a == DealAct.REQ_REFUND))
    register(on_agree_refund, DealCb.filter(F.a == DealAct.AGREE_REFUND))
    register(on_agree_refund_ok, DealCb.filter(F.a == DealAct.AGREE_REFUND_OK))
    register(on_object_refund, DealCb.filter(F.a == DealAct.OBJECT_REFUND))
    register(on_dispute, DealCb.filter(F.a == DealAct.DISPUTE))
    register(on_dispute_no_reason, DealCb.filter(F.a == DealAct.DISPUTE_NO_REASON))
    register(on_withdraw_dispute, DealCb.filter(F.a == DealAct.WITHDRAW_DISPUTE))
    register(on_refund_address, DealCb.filter(F.a == DealAct.REFUND_ADDRESS))
    register(
        on_refund_saved_address, WizCb.filter(F.a == WizAct.ADDRESS), StateFilter(RefundAddressSG.address)
    )
    register(on_leave_state, DealCb.filter(F.a == DealAct.VIEW), StateFilter(DisputeSG, RefundAddressSG))
    router.message.register(on_dispute_reason, DisputeSG.reason, F.text)
    router.message.register(on_refund_address_text, RefundAddressSG.address, F.text)
    return router
