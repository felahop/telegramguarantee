"""«Новая сделка» wizard (also used for one-line commands, repeat, templates and editing)."""

from __future__ import annotations

import contextlib
from typing import Protocol

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import WizAct, WizCb
from app.bot.context import BotContext
from app.bot.drafts import DealDraft, Step, applicable_steps, first_missing, previous_step
from app.bot.parsing import normalize_username, parse_hours
from app.bot.render import render_draft_preview
from app.bot.texts import esc
from app.bot.views import Screen, deal_view
from app.db.models import User
from app.domain.enums import FeePayer, Network, PartyRole
from app.domain.errors import AmountError, DomainError, Forbidden, ValidationFailed
from app.domain.fees import FeeBreakdown, FeePolicy, calculate_fees
from app.domain.money import parse_user_amount
from app.services.deals import DealChanges, NewDeal
from app.services.queries import (
    get_saved_address,
    recent_amounts,
    recent_counterparties,
    remember_address,
    saved_addresses,
)
from app.services.users import find_by_username


def need[T](value: T | None) -> T:
    """Draft invariants: guaranteed by ``first_missing`` before the step is shown."""
    if value is None:
        raise RuntimeError("wizard draft is incomplete")
    return value


class _Go(Protocol):
    async def __call__(self, step: Step | None = None, notice: str | None = None) -> None: ...


class Wizard(StatesGroup):
    role = State()
    network = State()
    amount = State()
    terms = State()
    hours = State()
    hours_custom = State()
    fee = State()
    address = State()
    counterparty = State()
    contact = State()
    preview = State()
    fields = State()


STATE_FOR_STEP = {
    Step.ROLE: Wizard.role,
    Step.NETWORK: Wizard.network,
    Step.AMOUNT: Wizard.amount,
    Step.TERMS: Wizard.terms,
    Step.HOURS: Wizard.hours,
    Step.FEE: Wizard.fee,
    Step.ADDRESS: Wizard.address,
    Step.COUNTERPARTY: Wizard.counterparty,
    Step.PREVIEW: Wizard.preview,
}


# ── entry points used by other routers ───────────────────────────────────────


async def start_wizard(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    ctx: BotContext,
    user: User,
    draft: DealDraft,
    *,
    msg_id: int | None = None,
    notice: str | None = None,
) -> None:
    await state.clear()
    await state.update_data(draft=draft.to_dict(), msg_id=msg_id)
    await show_step(bot, chat_id, state, ctx, user, draft, notice=notice)


async def show_step(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    ctx: BotContext,
    user: User,
    draft: DealDraft,
    step: Step | None = None,
    *,
    notice: str | None = None,
) -> None:
    missing = first_missing(draft)
    if step is None or (step is Step.PREVIEW and missing is not Step.PREVIEW):
        step = missing
    text, markup = await _build_step(ctx, user, draft, step)
    if notice:
        text = f"⚠️ {notice}\n\n{text}"
    await state.set_state(STATE_FOR_STEP[step])
    await state.update_data(draft=draft.to_dict())
    await Screen(bot, chat_id, state).show(text, markup)


# ── step screens ─────────────────────────────────────────────────────────────


def _quotes(ctx: BotContext, draft: DealDraft) -> dict[FeePayer, FeeBreakdown]:
    spec = ctx.spec(need(draft.network))
    policy = FeePolicy.from_settings(ctx.settings, spec)
    amount = spec.minor(need(draft.amount_decimal))
    quotes: dict[FeePayer, FeeBreakdown] = {}
    for payer in FeePayer:
        try:
            quotes[payer] = calculate_fees(amount, payer, policy)
        except AmountError:
            continue
    return quotes


async def _build_step(
    ctx: BotContext, user: User, draft: DealDraft, step: Step
) -> tuple[str, InlineKeyboardMarkup]:
    header = "✏️ <b>Изменение сделки</b>\n\n" if draft.editing else "➕ <b>Новая сделка</b>\n\n"
    if step is Step.ROLE:
        return header + T.ASK["role"], K.role_kb(draft)
    if step is Step.NETWORK:
        return header + T.ASK["network"], K.network_kb(draft, ctx.enabled_specs())
    if step is Step.AMOUNT:
        async with ctx.uow() as uow:
            recent = await recent_amounts(uow.session, user.id)
        current = f"\n\nСейчас: <b>{esc(draft.amount)} USDT</b>" if draft.amount else ""
        return header + T.ASK["amount"] + current, K.amount_kb(draft, recent)
    if step is Step.TERMS:
        current = f"\n\nСейчас:\n<blockquote>{esc(draft.terms)}</blockquote>" if draft.terms else ""
        return header + T.ASK["terms"] + current, K.terms_kb(draft)
    if step is Step.HOURS:
        current = f"\n\nСейчас: <b>{T.hours_label(draft.hours)}</b>" if draft.hours else ""
        return header + T.ASK["hours"] + current, K.hours_kb(draft)
    if step is Step.FEE:
        quotes = _quotes(ctx, draft)
        text = header + T.ASK["fee"]
        if not quotes:
            text += "\n\n⚠️ Сумма слишком мала: после комиссий продавцу ничего не останется. Измените сумму."
        return text, K.fee_kb(draft, quotes, ctx.spec(need(draft.network)))
    if step is Step.ADDRESS:
        network = need(draft.network)
        async with ctx.uow() as uow:
            saved = await saved_addresses(uow.session, user.id, network)
        text = header + T.ASK["address"].format(network=esc(ctx.spec(network).title))
        if draft.payout_address:
            text += f"\n\nСейчас: <code>{esc(draft.payout_address)}</code>"
        return text, K.address_kb(draft, saved)
    if step is Step.COUNTERPARTY:
        async with ctx.uow() as uow:
            partners = await recent_counterparties(uow.session, user.id)
        return header + T.ASK["counterparty"], K.counterparty_kb(draft, partners)
    return _preview(ctx, draft)


def _preview(ctx: BotContext, draft: DealDraft) -> tuple[str, InlineKeyboardMarkup]:
    spec = ctx.spec(need(draft.network))
    breakdown = calculate_fees(
        spec.minor(need(draft.amount_decimal)),
        need(draft.fee_payer),
        FeePolicy.from_settings(ctx.settings, spec),
    )
    text = render_draft_preview(
        draft,
        breakdown,
        spec,
        fee_bps=ctx.settings.fee_rate_bps,
        network_fee_payer=ctx.settings.network_fee_payer,
    )
    return text, K.preview_kb(draft)


# ── callbacks ────────────────────────────────────────────────────────────────


async def _draft(state: FSMContext) -> DealDraft:
    return DealDraft.from_dict((await state.get_data()).get("draft"))


async def on_wizard_button(
    callback: CallbackQuery, callback_data: WizCb, state: FSMContext, ctx: BotContext, user: User, bot: Bot
) -> None:
    draft = await _draft(state)
    chat_id = callback.from_user.id
    action, value = callback_data.a, callback_data.val

    async def go(step: Step | None = None, notice: str | None = None) -> None:
        await show_step(bot, chat_id, state, ctx, user, draft, step, notice=notice)

    if action is WizAct.CANCEL:
        await callback.answer("Отменено")
        await Screen(bot, chat_id, state).show("Создание сделки отменено.", K.main_menu())
        await state.clear()
        return
    await callback.answer()
    if action is WizAct.ROLE:
        draft.set_role(PartyRole(value))
        await go()
    elif action is WizAct.NETWORK:
        network = Network(value)
        if network not in ctx.chains.enabled_networks():
            await go(Step.NETWORK, notice="Эта сеть сейчас недоступна.")
            return
        draft.set_network(network)
        await go()
    elif action is WizAct.AMOUNT:
        await _set_amount(ctx, user, draft, value, go)
    elif action is WizAct.KEEP:
        await go()
    elif action is WizAct.GOTO:
        step = Step(value)
        await go(step if step in applicable_steps(draft) else None)
    elif action is WizAct.HOURS:
        draft.hours = int(value)
        await go()
    elif action is WizAct.HOURS_CUSTOM:
        await state.set_state(Wizard.hours_custom)
        await Screen(bot, chat_id, state).show(T.ASK["hours_custom"], K.custom_hours_kb())
    elif action is WizAct.FEE:
        draft.fee_payer = FeePayer(value)
        await go()
    elif action is WizAct.ADDRESS:
        await _set_saved_address(ctx, user, draft, int(value), go)
    elif action is WizAct.COUNTERPARTY:
        await _set_known_counterparty(ctx, user, draft, int(value), go)
    elif action is WizAct.OPEN_LINK:
        draft.set_counterparty(user_id=None, username=None, label=None)
        await go()
    elif action is WizAct.CONTACT:
        await state.set_state(Wizard.contact)
        await Screen(bot, chat_id, state).send_new(T.ASK["contact"], K.contact_request_kb())
    elif action is WizAct.BACK:
        current = Step(value)
        await go(previous_step(draft, current) or current)
    elif action is WizAct.FIELDS:
        await state.set_state(Wizard.fields)
        await Screen(bot, chat_id, state).show("Что изменить?", K.fields_kb(draft, applicable_steps(draft)))
    elif action is WizAct.CREATE:
        await _create(bot, chat_id, state, ctx, user, draft)


async def on_stale_wizard_button(callback: CallbackQuery) -> None:
    await callback.answer("Этот мастер уже закрыт. Начните заново: /start", show_alert=True)


async def _set_amount(ctx: BotContext, user: User, draft: DealDraft, raw: str, go: _Go) -> None:
    try:
        value = parse_user_amount(raw)
        async with ctx.uow() as uow:
            await uow.deals.check_amount(user.id, need(draft.network), value)
    except DomainError as exc:
        await go(Step.AMOUNT, notice=T.error_text(exc))
        return
    draft.amount = f"{value.normalize():f}"
    quotes_ok = bool(_quotes(ctx, draft))
    await go(
        None if quotes_ok else Step.AMOUNT,
        notice=None if quotes_ok else "Сумма слишком мала: после комиссий продавцу ничего не останется.",
    )


async def _set_saved_address(ctx: BotContext, user: User, draft: DealDraft, address_id: int, go: _Go) -> None:
    try:
        async with ctx.uow() as uow:
            saved = await get_saved_address(uow.session, user.id, address_id)
            draft.payout_address = await uow.deals.check_payout_address(need(draft.network), saved.address)
    except DomainError as exc:
        await go(Step.ADDRESS, notice=T.error_text(exc))
        return
    await go()


async def _set_known_counterparty(
    ctx: BotContext, user: User, draft: DealDraft, user_id: int, go: _Go
) -> None:
    if user_id == user.id:
        await go(Step.COUNTERPARTY, notice="Нельзя создать сделку с самим собой.")
        return
    async with ctx.uow() as uow:
        other = await uow.session.get(User, user_id)
    label = (f"@{other.username}" if other.username else other.first_name) if other else f"id{user_id}"
    draft.set_counterparty(user_id=user_id, username=None, label=label)
    await go()


async def _create(
    bot: Bot, chat_id: int, state: FSMContext, ctx: BotContext, user: User, draft: DealDraft
) -> None:
    if first_missing(draft) is not Step.PREVIEW:
        await show_step(bot, chat_id, state, ctx, user, draft)
        return
    role, network = need(draft.role), need(draft.network)
    try:
        async with ctx.uow() as uow:
            if draft.editing:
                deal = await uow.deals.get_for_user(user.id, need(draft.edit_deal_id))
                changes = DealChanges(
                    amount=need(draft.amount_decimal),
                    terms=need(draft.terms),
                    execution_hours=need(draft.hours),
                    fee_payer=need(draft.fee_payer),
                    seller_payout_address=draft.payout_address if role is PartyRole.SELLER else None,
                )
                deal = await uow.deals.edit_deal(
                    user.id, deal.id, version=need(draft.edit_version), changes=changes
                )
                headline = "💾 Изменения сохранены. Вторая сторона получит обновлённые условия."
            else:
                deal = await uow.deals.create_deal(
                    NewDeal(
                        creator_id=user.id,
                        creator_role=role,
                        network=network,
                        amount=need(draft.amount_decimal),
                        terms=need(draft.terms),
                        execution_hours=need(draft.hours),
                        fee_payer=need(draft.fee_payer),
                        counterparty_user_id=draft.counterparty_user_id,
                        counterparty_username=draft.counterparty_username,
                        seller_payout_address=draft.payout_address,
                        source=draft.source,
                    )
                )
                headline = _created_headline(deal.invited_user_id is not None)
            if draft.payout_address:
                await remember_address(uow.session, user.id, network, draft.payout_address, now=ctx.clock())
            text, markup = await deal_view(ctx, uow.session, deal, user.id)
    except DomainError as exc:
        await show_step(bot, chat_id, state, ctx, user, draft, Step.PREVIEW, notice=T.error_text(exc))
        return
    screen = Screen(bot, chat_id, state)
    await screen.show(f"{headline}\n\n{text}", markup)
    await state.clear()


def _created_headline(counterparty_known: bool) -> str:
    if counterparty_known:
        return "✅ Сделка создана и отправлена второй стороне."
    return "✅ Сделка создана. Отправьте приглашение второй стороне кнопкой ниже."


# ── text input steps ─────────────────────────────────────────────────────────


async def _consume(message: Message) -> None:
    """Remove the user's input to keep the chat tidy (bots may delete incoming private messages)."""
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()


async def on_amount(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    draft = await _draft(state)
    await _consume(message)

    async def go(step: Step | None = None, notice: str | None = None) -> None:
        await show_step(bot, message.chat.id, state, ctx, user, draft, step, notice=notice)

    await _set_amount(ctx, user, draft, message.text or "", go)


async def on_terms(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    draft = await _draft(state)
    await _consume(message)
    terms = (message.text or "").strip()
    limit = ctx.settings.terms_max_length
    if not 3 <= len(terms) <= limit:
        await show_step(
            bot,
            message.chat.id,
            state,
            ctx,
            user,
            draft,
            Step.TERMS,
            notice=f"Условия должны быть от 3 до {limit} символов.",
        )
        return
    draft.terms = terms
    await show_step(bot, message.chat.id, state, ctx, user, draft)


async def on_custom_hours(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    draft = await _draft(state)
    await _consume(message)
    try:
        draft.hours = parse_hours(message.text or "", max_hours=ctx.settings.execution_hours_max)
    except DomainError as exc:
        await Screen(bot, message.chat.id, state).show(
            f"⚠️ {T.error_text(exc)}\n\n{T.ASK['hours_custom']}", K.custom_hours_kb()
        )
        return
    await show_step(bot, message.chat.id, state, ctx, user, draft)


async def on_address(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    draft = await _draft(state)
    await _consume(message)
    try:
        async with ctx.uow() as uow:
            draft.payout_address = await uow.deals.check_payout_address(
                need(draft.network), message.text or ""
            )
    except DomainError as exc:
        await show_step(bot, message.chat.id, state, ctx, user, draft, Step.ADDRESS, notice=T.error_text(exc))
        return
    await show_step(bot, message.chat.id, state, ctx, user, draft)


async def on_counterparty(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    draft = await _draft(state)
    await _consume(message)
    try:
        username = normalize_username(message.text or "")
        if user.username and username.lower() == user.username.lower():
            raise ValidationFailed("self deal", field="counterparty")
        async with ctx.uow() as uow:
            known = await find_by_username(uow.session, username)
        if known is not None and known.is_banned:
            raise Forbidden("counterparty is banned")
    except DomainError as exc:
        await show_step(
            bot, message.chat.id, state, ctx, user, draft, Step.COUNTERPARTY, notice=T.error_text(exc)
        )
        return
    if known is not None:
        draft.set_counterparty(user_id=known.id, username=None, label=f"@{known.username or username}")
    else:
        draft.set_counterparty(user_id=None, username=username, label=f"@{username}")
    await show_step(bot, message.chat.id, state, ctx, user, draft)


async def on_contact_shared(
    message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot
) -> None:
    draft = await _draft(state)
    picked = need(message.users_shared).users[0]
    screen = Screen(bot, message.chat.id, state)
    if picked.user_id == user.id:
        await screen.send_new("Нельзя создать сделку с самим собой.", ReplyKeyboardRemove())
        await show_step(bot, message.chat.id, state, ctx, user, draft, Step.COUNTERPARTY)
        return
    label = f"@{picked.username}" if picked.username else (picked.first_name or f"id{picked.user_id}")
    # request_users returns a user_id even if that person never opened the bot: the deal is bound
    # to this id, and only they can accept it.
    draft.set_counterparty(user_id=picked.user_id, username=None, label=label)
    await screen.send_new(f"Контрагент: {esc(label)}", ReplyKeyboardRemove())
    await show_step(bot, message.chat.id, state, ctx, user, draft)


async def on_contact_cancel(
    message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot
) -> None:
    draft = await _draft(state)
    await Screen(bot, message.chat.id, state).send_new("Выбор контакта отменён.", ReplyKeyboardRemove())
    await show_step(bot, message.chat.id, state, ctx, user, draft, Step.COUNTERPARTY)


async def on_unexpected_input(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой в сообщении выше.")


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="wizard")
    router.callback_query.register(on_wizard_button, WizCb.filter(), StateFilter(Wizard))
    router.callback_query.register(on_stale_wizard_button, WizCb.filter())
    router.message.register(on_amount, Wizard.amount, F.text)
    router.message.register(on_terms, Wizard.terms, F.text)
    router.message.register(on_custom_hours, Wizard.hours_custom, F.text)
    router.message.register(on_address, Wizard.address, F.text)
    router.message.register(on_counterparty, Wizard.counterparty, F.text)
    router.message.register(on_contact_shared, Wizard.contact, F.users_shared)
    router.message.register(on_contact_cancel, Wizard.contact)
    router.message.register(on_unexpected_input, StateFilter(Wizard))
    return router
