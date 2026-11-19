"""Deal card actions during negotiation: view, accept (with payout address), decline/cancel,
confirm the open-link candidate, edit, repeat, save as template."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import DealAct, DealCb, WizAct, WizCb
from app.bot.context import BotContext
from app.bot.handlers.wizard import start_wizard
from app.bot.texts import esc
from app.bot.views import deal_view, draft_for_edit, draft_for_repeat
from app.db.models import Deal, User
from app.domain.enums import EDITABLE_STATUSES, DealStatus, PartyRole, Provider
from app.domain.errors import DomainError, Forbidden, InvalidTransition
from app.services.queries import get_saved_address, remember_address, save_template, saved_addresses
from app.services.users import get_active_user


class AcceptSG(StatesGroup):
    address = State()


async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if not isinstance(callback.message, Message):  # inaccessible (too old) message
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def _load(ctx: BotContext, user: User, deal_id: int) -> Deal:
    async with ctx.uow() as uow:
        return await uow.deals.get_for_user(user.id, deal_id)


async def on_view(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_invitee(user.id, callback_data.id)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer()
    await _edit(callback, text, markup)


async def on_accept(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_invitee(user.id, callback_data.id)
        needs_address = (
            deal.creator_role is PartyRole.BUYER
            and deal.provider is Provider.ONCHAIN
            and not deal.seller_payout_address
        )
        saved = await saved_addresses(uow.session, user.id, deal.network) if needs_address else []
    if needs_address:
        await state.set_state(AcceptSG.address)
        await state.update_data(accept_deal_id=deal.id, accept_version=callback_data.v)
        await callback.answer()
        await _edit(
            callback,
            T.ASK["address"].format(network=esc(ctx.spec(deal.network).title)),
            K.accept_address_kb(saved),
        )
        return
    await _do_accept(callback, ctx, user, deal.id, callback_data.v, None)


async def _do_accept(
    target: CallbackQuery | Message,
    ctx: BotContext,
    user: User,
    deal_id: int,
    version: int,
    address: str | None,
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.accept_deal(user.id, deal_id, version=version, payout_address=address)
        if address:
            await remember_address(uow.session, user.id, deal.network, address, now=ctx.clock())
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    headline = (
        "⏳ Создатель сделки подтвердит, что вы — его контрагент."
        if deal.status is DealStatus.CREATED
        else "🤝 Вы приняли условия."
    )
    if isinstance(target, CallbackQuery):
        await target.answer()
        await _edit(target, f"{headline}\n\n{text}", markup)
    else:
        await target.answer(f"{headline}\n\n{text}", reply_markup=markup)


async def on_accept_saved_address(
    callback: CallbackQuery, callback_data: WizCb, ctx: BotContext, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    async with ctx.uow() as uow:
        saved = await get_saved_address(uow.session, user.id, int(callback_data.val))
    await state.clear()
    await _do_accept(callback, ctx, user, data["accept_deal_id"], data["accept_version"], saved.address)


async def on_accept_cancel(callback: CallbackQuery, ctx: BotContext, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer("Отменено")
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_invitee(user.id, data["accept_deal_id"])
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await _edit(callback, text, markup)


async def on_accept_address(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        await _do_accept(
            message, ctx, user, data["accept_deal_id"], data["accept_version"], message.text or ""
        )
    except DomainError as exc:
        await message.answer(f"⚠️ {T.error_text(exc)}")
        return
    await state.clear()


async def on_cancel_ask(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_invitee(user.id, callback_data.id)
        text, _ = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer()
    await _edit(callback, f"{text}\n\n❓ <b>Отменить сделку?</b>", K.cancel_confirm_kb(deal))


async def on_cancel(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.cancel(user.id, callback_data.id)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer("Сделка отменена")
    await _edit(callback, text, markup)


async def on_candidate(callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User) -> None:
    approve = callback_data.a is DealAct.CONFIRM_CANDIDATE
    async with ctx.uow() as uow:
        deal = await uow.deals.confirm_counterparty(user.id, callback_data.id, approve=approve)
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await callback.answer("Контрагент подтверждён" if approve else "Кандидат отклонён")
    await _edit(callback, text, markup)


async def on_edit(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    deal = await _load(ctx, user, callback_data.id)
    if deal.creator_id != user.id:
        raise Forbidden("only the creator edits the deal")
    if deal.status not in EDITABLE_STATUSES:
        raise InvalidTransition("deal can no longer be edited")
    await callback.answer()
    msg_id = callback.message.message_id if isinstance(callback.message, Message) else None
    await start_wizard(bot, user.id, state, ctx, user, draft_for_edit(deal), msg_id=msg_id)


async def on_repeat(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    async with ctx.uow() as uow:
        await get_active_user(uow.session, user.id)
        deal = await uow.deals.get_for_user(user.id, callback_data.id)
        other_id = deal.counterparty_of(user.id)
        other = await uow.session.get(User, other_id) if other_id else None
    label = (f"@{other.username}" if other.username else other.first_name) if other else None
    await callback.answer()
    await start_wizard(bot, user.id, state, ctx, user, draft_for_repeat(deal, user.id, label))


async def on_save_template(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, callback_data.id)
        template = await save_template(uow.session, user.id, deal)
    await callback.answer(f"Шаблон «{template.name[:40]}» сохранён", show_alert=True)


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="deal_card")
    router.callback_query.register(on_view, DealCb.filter(F.a == DealAct.VIEW))
    router.callback_query.register(on_accept, DealCb.filter(F.a == DealAct.ACCEPT))
    router.callback_query.register(
        on_accept_saved_address, WizCb.filter(F.a == WizAct.ADDRESS), StateFilter(AcceptSG.address)
    )
    router.callback_query.register(
        on_accept_cancel, WizCb.filter(F.a == WizAct.CANCEL), StateFilter(AcceptSG.address)
    )
    router.message.register(on_accept_address, AcceptSG.address, F.text)
    router.callback_query.register(on_cancel_ask, DealCb.filter(F.a == DealAct.CANCEL))
    router.callback_query.register(on_cancel, DealCb.filter(F.a == DealAct.CANCEL_CONFIRM))
    router.callback_query.register(
        on_candidate, DealCb.filter(F.a.in_({DealAct.CONFIRM_CANDIDATE, DealAct.REJECT_CANDIDATE}))
    )
    router.callback_query.register(on_edit, DealCb.filter(F.a == DealAct.EDIT))
    router.callback_query.register(on_repeat, DealCb.filter(F.a == DealAct.REPEAT))
    router.callback_query.register(on_save_template, DealCb.filter(F.a == DealAct.SAVE_TEMPLATE))
    return router
