"""Admin panel (/admin): disputes, payout approvals, failed payouts, unidentified payments, balances,
search, bans, chain status, TOTP setup (/totp).

Every money action (dispute decision, payout approval/hold, assigning or returning an unidentified
payment) goes through one TOTP gate: the pending operation is kept in the admin's FSM state, the
code is verified (once-only), then the operation runs in its own transaction.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from app.bot import admin_keyboards as AK
from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.admin_keyboards import PAGE, AdmAct, AdmCb
from app.bot.admin_render import (
    render_balances,
    render_chains,
    render_dossier,
    render_menu,
    render_payout,
    render_reconcile_report,
    render_reconciliation,
    render_resolution_preview,
    render_transfer,
    render_user,
)
from app.bot.context import BotContext
from app.bot.texts import esc
from app.bot.views import deal_view
from app.db.models import ChainCursor, Deal, DealMessage, Dispute, IncomingTransfer, Payout, User
from app.domain.enums import DisputeDecision, Network, PayoutStatus, Provider, UserRole
from app.domain.errors import DomainError, NotFound
from app.domain.money import NATIVE_DECIMALS
from app.logging import get_logger
from app.services.admin import ADMINS, ARBITERS, AdminService
from app.services.payments import CRYPTOBOT_WALLET
from app.services.reconciliation import balance_sources, reconcile
from app.services.totp import TotpError, TotpService
from app.services.users import find_by_username

log = get_logger(__name__)
MAX_TOTP_FAILURES = 3


class StaffFilter(Filter):
    def __init__(self, roles: frozenset[UserRole]) -> None:
        self._roles = roles

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.role in self._roles and not user.is_banned


class AdminSG(StatesGroup):
    totp = State()
    totp_enroll = State()
    split_pct = State()
    comment = State()
    reject_reason = State()
    assign_code = State()
    return_recipient = State()
    search = State()
    message = State()
    ban_reason = State()


TOTP_REASONS = {
    "not_configured": "TOTP не настроен на сервере (нет SECRETS_ENCRYPTION_KEY).",
    "not_enrolled": "Сначала подключите TOTP командой /totp.",
    "invalid": "Неверный код.",
    "replay": "Этот код уже использован — дождитесь следующего.",
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _svc(ctx: BotContext, session: Any) -> AdminService:
    return AdminService(session, ctx.settings, ctx.chains, clock=ctx.clock, gateway=ctx.gateway)


async def _send(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.bot is not None:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=reply_markup)


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await _send(callback, text, reply_markup=markup)


def _decimals_for(ctx: BotContext) -> Any:
    def decimals(asset: str) -> int:
        coin, _, network = asset.partition("@")
        if coin == "USDT":
            return ctx.spec(Network(network)).decimals
        return NATIVE_DECIMALS.get(coin, 6)

    return decimals


def _totp(ctx: BotContext) -> TotpService:
    return TotpService(ctx.settings, ctx.kv, ctx.clock)


# ── menu and TOTP setup ──────────────────────────────────────────────────────


async def _menu_text(ctx: BotContext) -> str:
    async with ctx.uow() as uow:
        return render_menu(await _svc(ctx, uow.session).counts())


async def on_admin(message: Message, ctx: BotContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await _menu_text(ctx), reply_markup=AK.menu())


async def on_menu(callback: CallbackQuery, ctx: BotContext, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, await _menu_text(ctx), AK.menu())


async def on_totp_setup(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    totp = _totp(ctx)
    if not totp.required:
        await message.answer("TOTP выключен настройкой ADMIN_TOTP_REQUIRED.")
        return
    if totp.enrolled(user):
        await message.answer("🔐 TOTP уже подключён. Сброс — командой CLI <code>reset-totp</code>.")
        return
    try:
        async with ctx.uow() as uow:
            fresh = await uow.session.get(User, user.id, with_for_update=True)
            if fresh is None:
                raise NotFound("user")
            secret, uri = totp.start_enrollment(fresh)
    except TotpError as exc:
        await message.answer(TOTP_REASONS.get(str(exc.context.get("reason")), "Ошибка TOTP."))
        return
    await state.set_state(AdminSG.totp_enroll)
    await message.answer(
        "🔐 <b>Подключение TOTP</b>\n\nДобавьте ключ в приложение-аутентификатор (Google Authenticator, "
        f"Aegis, 1Password…):\n\n<code>{secret}</code>\n\nили ссылкой:\n<code>{esc(uri)}</code>\n\n"
        "Затем пришлите текущий 6-значный код. Удалите это сообщение после настройки.",
        protect_content=True,
    )


async def on_totp_enroll_code(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    try:
        async with ctx.uow() as uow:
            fresh = await uow.session.get(User, user.id, with_for_update=True)
            if fresh is None:
                raise NotFound("user")
            await _totp(ctx).confirm_enrollment(fresh, message.text or "")
    except TotpError as exc:
        await message.answer(TOTP_REASONS.get(str(exc.context.get("reason")), "Ошибка TOTP."))
        return
    await state.clear()
    await message.answer("✅ TOTP подключён. Денежные действия теперь подтверждаются кодом.")


# ── the TOTP gate ────────────────────────────────────────────────────────────


async def _gate(
    bot: Bot,
    chat_id: int,
    ctx: BotContext,
    admin: User,
    state: FSMContext,
    op: str,
    params: dict[str, Any],
    summary: str,
) -> None:
    totp = _totp(ctx)
    if not totp.required:
        await state.clear()
        await bot.send_message(chat_id, await _run_op(ctx, admin.id, op, params), reply_markup=AK.only_back())
        return
    if not totp.enrolled(admin):
        await state.clear()
        await bot.send_message(chat_id, TOTP_REASONS["not_enrolled"], reply_markup=AK.only_back())
        return
    await state.set_state(AdminSG.totp)
    await state.update_data(op=op, params=params, totp_failures=0)
    await bot.send_message(
        chat_id,
        f"🔐 {summary}\n\nВведите 6-значный код из приложения-аутентификатора.",
        reply_markup=AK.cancel(),
    )


async def on_totp_code(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        await _totp(ctx).verify(user, message.text or "")
    except TotpError as exc:
        failures = int(data.get("totp_failures", 0)) + 1
        reason = str(exc.context.get("reason"))
        if failures >= MAX_TOTP_FAILURES or reason in ("not_enrolled", "not_configured"):
            await state.clear()
            log.warning("admin_totp_failed", admin_id=user.id, op=data.get("op"), reason=reason)
            await message.answer(
                f"{TOTP_REASONS.get(reason, 'Ошибка TOTP.')} Действие отменено.", reply_markup=AK.only_back()
            )
            return
        await state.update_data(totp_failures=failures)
        await message.answer(f"{TOTP_REASONS.get(reason, 'Ошибка TOTP.')} Попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer(await _run_op(ctx, user.id, data["op"], data["params"]), reply_markup=AK.only_back())


async def _run_op(ctx: BotContext, admin_id: int, op: str, params: dict[str, Any]) -> str:
    try:
        async with ctx.uow() as uow:
            svc = _svc(ctx, uow.session)
            if op == "resolve":
                deal = await uow.deals.resolve_dispute(
                    admin_id,
                    params["deal_id"],
                    decision=DisputeDecision(params["decision"]),
                    comment=params["comment"],
                    buyer_share_bps=params.get("bps"),
                    keep_fee=params["keep_fee"],
                )
                return f"⚖️ Решение по сделке <code>{deal.code}</code> принято. Выплаты поставлены в очередь."
            if op == "approve":
                payout = await svc.approve_payout(admin_id, params["payout_id"])
                return f"✅ Выплата #{payout.id}: {payout.status.value}."
            if op == "reject":
                payout = await svc.reject_payout(admin_id, params["payout_id"], params["reason"])
                return f"⏸ Выплата #{payout.id} отложена."
            if op == "assign":
                deal = await svc.assign_transfer(admin_id, params["transfer_id"], params["code"])
                return f"🔗 Платёж привязан к сделке <code>{deal.code}</code>: {T.STATUS[deal.status]}."
            if op == "return":
                payout = await svc.return_transfer(
                    admin_id,
                    params["transfer_id"],
                    recipient_user_id=params.get("user_id"),
                    recipient_address=params.get("address"),
                )
                return f"↩️ Возврат поставлен в очередь (выплата #{payout.id})."
            if op == "resume":
                pause = await svc.resume_payouts(admin_id, params["wallet"])
                return f"▶️ Выплаты с кошелька {esc(pause.wallet)} возобновлены."
    except DomainError as exc:
        return f"⚠️ {T.error_text(exc)}"
    raise ValueError(f"unknown admin op {op!r}")


async def on_cancel(callback: CallbackQuery, ctx: BotContext, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, await _menu_text(ctx), AK.menu())


# ── disputes ─────────────────────────────────────────────────────────────────


async def on_disputes(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    async with ctx.uow() as uow:
        items = await _svc(ctx, uow.session).open_disputes(offset=callback_data.p * PAGE, limit=PAGE + 1)
    text = "⚖️ <b>Открытые споры</b> (старые сверху)" if items else "Открытых споров нет."
    await _show(callback, text, AK.disputes_list(items[:PAGE], callback_data.p, len(items) > PAGE))


async def _dossier(ctx: BotContext, deal_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with ctx.uow() as uow:
        d = await _svc(ctx, uow.session).dossier(deal_id)
    return render_dossier(d), AK.dossier(d.deal, d.dispute)


async def on_dispute(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, state: FSMContext
) -> None:
    await state.clear()
    text, markup = await _dossier(ctx, callback_data.id)
    await _show(callback, text, markup)


async def on_take(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        await _svc(ctx, uow.session).take_dispute(user.id, callback_data.id)
    text, markup = await _dossier(ctx, callback_data.id)
    await _show(callback, text, markup)


async def on_full_chat(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User, bot: Bot
) -> None:
    async with ctx.uow() as uow:
        await _svc(ctx, uow.session).require(user.id, ARBITERS)
        messages = list(
            (
                await uow.session.execute(
                    sa.select(DealMessage)
                    .where(DealMessage.deal_id == callback_data.id)
                    .order_by(DealMessage.id)
                )
            ).scalars()
        )
    await callback.answer()
    if not messages:
        await bot.send_message(user.id, "Чат сделки пуст.")
        return
    chunk: list[str] = []
    for m in messages:
        line = f"<i>{m.created_at:%d.%m %H:%M}</i> <b>{m.sender_role}</b>: {esc(m.text or '')}"
        if m.file_id and m.file_type in ("photo", "document", "video"):
            send = {"photo": bot.send_photo, "document": bot.send_document, "video": bot.send_video}[
                m.file_type
            ]
            await send(user.id, m.file_id, caption=line[:1000])  # type: ignore[operator]
            continue
        chunk.append(line)
        if sum(len(x) for x in chunk) > 3500:
            await bot.send_message(user.id, "\n".join(chunk))
            chunk = []
    if chunk:
        await bot.send_message(user.id, "\n".join(chunk))


async def _preview(
    ctx: BotContext, deal_id: int, decision: str, bps: int, keep_fee: bool
) -> tuple[str, InlineKeyboardMarkup]:
    async with ctx.uow() as uow:
        deal = await uow.session.get(Deal, deal_id)
        if deal is None:
            raise NotFound("deal not found")
        preview = await uow.deals.preview_resolution(
            deal_id,
            decision=DisputeDecision(decision),
            buyer_share_bps=bps if decision == "split" else None,
            keep_fee=keep_fee,
        )
    text = render_resolution_preview(
        deal,
        decision=decision,
        bps=preview.buyer_share_bps,
        keep_fee=keep_fee,
        escrow=preview.escrow,
        s=preview.settlement,
    )
    return text, AK.resolution(deal, decision, preview.buyer_share_bps, keep_fee)


async def on_decide(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    keep = ctx.settings.dispute_keeps_fee_default
    text, markup = await _preview(ctx, callback_data.id, callback_data.x, callback_data.p, keep)
    await _show(callback, text, markup)


async def on_fee_toggle(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    decision, _, keep = callback_data.x.partition(".")
    text, markup = await _preview(ctx, callback_data.id, decision, callback_data.p, keep == "1")
    await _show(callback, text, markup)


async def on_split_custom(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AdminSG.split_pct)
    await state.update_data(deal_id=callback_data.id)
    await callback.answer()
    await _send(
        callback,
        "Какой процент получает покупатель? Число от 1 до 99, например <code>70</code>.",
        reply_markup=AK.cancel(),
    )


async def on_split_pct(message: Message, ctx: BotContext, state: FSMContext) -> None:
    raw = (message.text or "").strip().rstrip("%")
    if not raw.isdigit() or not 1 <= int(raw) <= 99:
        await message.answer("Нужно целое число от 1 до 99.")
        return
    deal_id = (await state.get_data())["deal_id"]
    await state.clear()
    text, markup = await _preview(
        ctx, deal_id, "split", int(raw) * 100, ctx.settings.dispute_keeps_fee_default
    )
    await message.answer(text, reply_markup=markup)


async def on_confirm_decision(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    decision, _, keep = callback_data.x.partition(".")
    await state.set_state(AdminSG.comment)
    await state.update_data(
        resolution={
            "deal_id": callback_data.id,
            "decision": decision,
            "bps": callback_data.p if decision == "split" else None,
            "keep_fee": keep == "1",
        }
    )
    await callback.answer()
    await _send(
        callback,
        "✍️ Напишите обоснование решения одним сообщением — его увидят обе стороны.",
        reply_markup=AK.cancel(),
    )


async def on_comment(message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot) -> None:
    comment = (message.text or "").strip()
    if len(comment) < 3:
        await message.answer("Комментарий слишком короткий.")
        return
    params = dict((await state.get_data())["resolution"], comment=comment[:2000])
    summary = {"buyer": "всё покупателю", "seller": "всё продавцу"}.get(
        params["decision"], f"покупателю {(params['bps'] or 0) / 100:g}%"
    )
    await _gate(
        bot, message.chat.id, ctx, user, state, "resolve", params, f"Подтвердите решение спора: {summary}."
    )


async def on_message_parties(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AdminSG.message)
    await state.update_data(deal_id=callback_data.id)
    await callback.answer()
    await _send(
        callback,
        "✉️ Напишите сообщение — бот отправит его обеим сторонам от имени арбитра.",
        reply_markup=AK.cancel(),
    )


async def on_parties_text(message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot) -> None:
    deal_id = (await state.get_data())["deal_id"]
    text = (message.text or "").strip()[:3000]
    async with ctx.uow() as uow:
        deal = await _svc(ctx, uow.session).log_arbiter_message(user.id, deal_id, text)
    await state.clear()
    delivered = 0
    for party in (deal.buyer_id, deal.seller_id):
        if party is None:
            continue
        try:
            await bot.send_message(
                party,
                f"⚖️ <b>Сообщение арбитра</b> по сделке <code>{esc(deal.code)}</code>:\n\n"
                f"{esc(text)}\n\nОтветить можно в чате сделки.",
                reply_markup=K.chat_relay_kb(deal),
            )
            delivered += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
    await message.answer(f"Отправлено сторонам: {delivered} из 2.", reply_markup=AK.only_back())


# ── payouts ──────────────────────────────────────────────────────────────────


async def _payouts_list(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, statuses: list[PayoutStatus], title: str
) -> None:
    async with ctx.uow() as uow:
        items = await _svc(ctx, uow.session).payouts_in(
            statuses, offset=callback_data.p * PAGE, limit=PAGE + 1
        )
    await _show(
        callback,
        title if items else "Пусто.",
        AK.payouts_list(items[:PAGE], callback_data.a, callback_data.p, len(items) > PAGE),
    )


async def on_approvals(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    await _payouts_list(
        callback,
        callback_data,
        ctx,
        [PayoutStatus.AWAITING_APPROVAL],
        "💸 <b>Выплаты, ждущие подтверждения</b>",
    )


async def on_failed(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    await _payouts_list(
        callback,
        callback_data,
        ctx,
        [PayoutStatus.FAILED_RETRYABLE, PayoutStatus.FAILED_FINAL],
        "🚨 <b>Проблемные выплаты</b>",
    )


async def _payout_card(ctx: BotContext, payout_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with ctx.uow() as uow:
        payout = await uow.session.get(Payout, payout_id)
        if payout is None:
            raise NotFound("payout not found")
        deal = await uow.session.get(Deal, payout.deal_id) if payout.deal_id else None
        ids = [i for i in (payout.recipient_user_id,) if i]
        users = {
            u.id: u for u in (await uow.session.execute(sa.select(User).where(User.id.in_(ids)))).scalars()
        }
    return render_payout(payout, deal, users), AK.payout(payout)


async def on_payout(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    text, markup = await _payout_card(ctx, callback_data.id)
    await _show(callback, text, markup)


async def on_approve(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    await callback.answer()
    await _gate(
        bot,
        user.id,
        ctx,
        user,
        state,
        "approve",
        {"payout_id": callback_data.id},
        f"Подтвердите выплату #{callback_data.id}.",
    )


async def on_reject(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AdminSG.reject_reason)
    await state.update_data(payout_id=callback_data.id)
    await callback.answer()
    await _send(
        callback,
        "Почему выплата откладывается? Причину увидят другие админы.",
        reply_markup=AK.cancel(),
    )


async def on_reject_reason(
    message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    payout_id = (await state.get_data())["payout_id"]
    await _gate(
        bot,
        message.chat.id,
        ctx,
        user,
        state,
        "reject",
        {"payout_id": payout_id, "reason": (message.text or "")[:300]},
        f"Отложить выплату #{payout_id}.",
    )


async def on_retry(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        await _svc(ctx, uow.session).retry_payout(user.id, callback_data.id)
    text, markup = await _payout_card(ctx, callback_data.id)
    await _show(callback, text, markup)


# ── unidentified payments ────────────────────────────────────────────────────


async def on_unidentified(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    async with ctx.uow() as uow:
        items = await _svc(ctx, uow.session).unidentified(offset=callback_data.p * PAGE, limit=PAGE + 1)
    await _show(
        callback,
        "❓ <b>Неопознанные платежи</b>" if items else "Неопознанных платежей нет.",
        AK.transfers_list(items[:PAGE], callback_data.p, len(items) > PAGE),
    )


async def on_transfer(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    async with ctx.uow() as uow:
        transfer = await uow.session.get(IncomingTransfer, callback_data.id)
        if transfer is None:
            raise NotFound("payment not found")
        candidates = await _svc(ctx, uow.session).candidates(transfer)
    decimals = ctx.spec(transfer.network).decimals
    await _show(callback, render_transfer(transfer, candidates, decimals), AK.transfer(transfer))


async def on_assign(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AdminSG.assign_code)
    await state.update_data(transfer_id=callback_data.id)
    await callback.answer()
    await _send(
        callback,
        "Код сделки, к которой относится платёж (например <code>D7KQ2MXA</code>):",
        reply_markup=AK.cancel(),
    )


async def on_assign_code(message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot) -> None:
    transfer_id = (await state.get_data())["transfer_id"]
    code = (message.text or "").strip().upper().lstrip("#")
    await _gate(
        bot,
        message.chat.id,
        ctx,
        user,
        state,
        "assign",
        {"transfer_id": transfer_id, "code": code},
        f"Привязать платёж #{transfer_id} к сделке <code>{esc(code)}</code>.",
    )


async def on_return(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, state: FSMContext
) -> None:
    async with ctx.uow() as uow:
        transfer = await uow.session.get(IncomingTransfer, callback_data.id)
    if transfer is None:
        raise NotFound("payment not found")
    await state.set_state(AdminSG.return_recipient)
    await state.update_data(transfer_id=transfer.id, provider=transfer.provider.value)
    await callback.answer()
    hint = (
        "Telegram ID или @username плательщика (вернём в CryptoBot):"
        if transfer.provider is Provider.CRYPTOBOT
        else "Адрес, на который вернуть платёж (проверьте, что это кошелёк плательщика):"
    )
    await _send(callback, hint, reply_markup=AK.cancel())


async def on_return_recipient(
    message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    params: dict[str, Any] = {"transfer_id": data["transfer_id"]}
    if data["provider"] == Provider.CRYPTOBOT.value:
        if raw.isdigit():
            params["user_id"] = int(raw)
        else:
            async with ctx.uow() as uow:
                found = await find_by_username(uow.session, raw)
            if found is None:
                await message.answer("Пользователь не найден в боте. Пришлите числовой Telegram ID.")
                return
            params["user_id"] = found.id
        target = f"пользователю id {params['user_id']}"
    else:
        params["address"] = raw
        target = f"на адрес <code>{esc(raw)}</code>"
    await _gate(
        bot,
        message.chat.id,
        ctx,
        user,
        state,
        "return",
        params,
        f"Вернуть платёж #{data['transfer_id']} {target}.",
    )


# ── overview, search, users ──────────────────────────────────────────────────


async def _balances_view(ctx: BotContext) -> tuple[str, InlineKeyboardMarkup]:
    async with ctx.uow() as uow:
        svc = _svc(ctx, uow.session)
        rows = await svc.balances()
        status = await svc.reconciliation_status()
    live: dict[str, tuple[str, str]] | None = None
    if ctx.cryptopay is not None:
        try:
            live = {
                b.currency_code: (f"{b.available:f}", f"{b.onhold:f}")
                for b in await ctx.cryptopay.get_balance()
            }
        except Exception as exc:  # the panel must open even when the provider is down
            log.warning("admin_balance_fetch_failed", error=repr(exc))
    decimals = _decimals_for(ctx)
    text = render_balances(rows, live, decimals) + "\n" + render_reconciliation(status, decimals)
    wallets = {r.wallet for r in status.runs} | set(status.pauses)
    if ctx.cryptopay is not None:
        wallets.add(CRYPTOBOT_WALLET)
    return text, AK.balances(sorted(wallets), list(status.pauses))


async def on_balances(callback: CallbackQuery, ctx: BotContext) -> None:
    await _show(callback, *await _balances_view(ctx))


async def on_pause(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User) -> None:
    """Stopping payouts is immediate and needs no TOTP: it can only make things safer."""
    async with ctx.uow() as uow:
        created = await _svc(ctx, uow.session).pause_payouts(
            user.id, callback_data.x, f"остановлено вручную админом {user.id}"
        )
    log.warning("admin_paused_payouts", admin_id=user.id, wallet=callback_data.x)
    await callback.answer(
        "⏸ Выплаты остановлены." if created else "Выплаты уже остановлены.", show_alert=True
    )
    text, markup = await _balances_view(ctx)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            log.info("admin_balances_not_edited", error=str(exc))


async def on_resume(
    callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    await callback.answer()
    await _gate(
        bot,
        user.id,
        ctx,
        user,
        state,
        "resume",
        {"wallet": callback_data.x},
        f"Возобновить выплаты с кошелька {esc(callback_data.x)}. Если недостача не устранена "
        "(пополнение записано командой record-funding), следующая сверка снова их остановит.",
    )


async def on_reconcile(callback: CallbackQuery, ctx: BotContext) -> None:
    await callback.answer("Сверяю…")
    report = await reconcile(
        ctx.sessionmaker,
        ctx.settings,
        ctx.chains,
        sources=balance_sources(ctx.chains, ctx.cryptopay),
        now=ctx.clock(),
    )
    await _send(callback, render_reconcile_report(report), reply_markup=AK.only_back())


async def on_chains(callback: CallbackQuery, ctx: BotContext) -> None:
    async with ctx.uow() as uow:
        cursors = list((await uow.session.execute(sa.select(ChainCursor))).scalars())
    await _show(callback, render_chains(cursors), AK.only_back())


async def on_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSG.search)
    await callback.answer()
    await _send(
        callback,
        "🔎 Код сделки, @username, Telegram ID или tx hash / номер счёта:",
        reply_markup=AK.cancel(),
    )


async def on_search_query(message: Message, ctx: BotContext, state: FSMContext) -> None:
    await state.clear()
    async with ctx.uow() as uow:
        result = await _svc(ctx, uow.session).search(message.text or "")
    if result.empty:
        await message.answer("Ничего не найдено.", reply_markup=AK.only_back())
        return
    lines = [
        f"Найдено: сделок {len(result.deals)}, пользователей {len(result.users)}, "
        f"платежей {len(result.transfers)}."
    ]
    lines += [f"• платёж #{t.id} · {t.network.value} · {t.match_status.value}" for t in result.transfers]
    await message.answer("\n".join(lines), reply_markup=AK.search_results(result.deals, result.users))


async def on_deal(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, callback_data.id)
        text, _ = await deal_view(ctx, uow.session, deal, user.id)
        has_dispute = bool(
            await uow.session.scalar(
                sa.select(sa.func.count()).select_from(Dispute).where(Dispute.deal_id == deal.id)
            )
        )
    await _show(callback, text, AK.deal_admin(deal, has_dispute))


async def on_user(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext) -> None:
    async with ctx.uow() as uow:
        target = await uow.session.get(User, callback_data.id)
        if target is None:
            raise NotFound("user not found")
        deals = list(
            (
                await uow.session.execute(
                    sa.select(Deal)
                    .where(sa.or_(Deal.buyer_id == target.id, Deal.seller_id == target.id))
                    .order_by(Deal.id.desc())
                    .limit(5)
                )
            ).scalars()
        )
    await _show(callback, render_user(target, deals), AK.user(target))


async def on_ban(callback: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AdminSG.ban_reason)
    await state.update_data(user_id=callback_data.id)
    await callback.answer()
    await _send(callback, "Причина блокировки:", reply_markup=AK.cancel())


async def on_ban_reason(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    target_id = (await state.get_data())["user_id"]
    await state.clear()
    async with ctx.uow() as uow:
        target = await _svc(ctx, uow.session).set_ban(
            user.id, target_id, banned=True, reason=message.text or ""
        )
    await message.answer(f"🚫 Пользователь id {target.id} заблокирован.", reply_markup=AK.only_back())


async def on_unban(callback: CallbackQuery, callback_data: AdmCb, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        target = await _svc(ctx, uow.session).set_ban(user.id, callback_data.id, banned=False)
        deals = list(
            (
                await uow.session.execute(
                    sa.select(Deal)
                    .where(sa.or_(Deal.buyer_id == target.id, Deal.seller_id == target.id))
                    .order_by(Deal.id.desc())
                    .limit(5)
                )
            ).scalars()
        )
    await _show(callback, render_user(target, deals), AK.user(target))


def build_router() -> Router:
    router = Router(name="admin")
    staff = StaffFilter(ARBITERS)
    admins = StaffFilter(ADMINS)
    cb = router.callback_query.register
    msg = router.message.register

    msg(on_admin, Command("admin"), staff)
    msg(on_totp_setup, Command("totp"), staff)
    msg(on_totp_enroll_code, AdminSG.totp_enroll, F.text, staff)
    msg(on_totp_code, AdminSG.totp, F.text, staff)

    cb(on_cancel, AdmCb.filter(F.a == AdmAct.CANCEL), staff)
    cb(on_menu, AdmCb.filter(F.a == AdmAct.MENU), staff)
    # disputes: admins and arbitrators
    cb(on_disputes, AdmCb.filter(F.a == AdmAct.DISPUTES), staff)
    cb(on_dispute, AdmCb.filter(F.a == AdmAct.DISPUTE), staff)
    cb(on_take, AdmCb.filter(F.a == AdmAct.TAKE), staff)
    cb(on_full_chat, AdmCb.filter(F.a == AdmAct.FULL_CHAT), staff)
    cb(on_decide, AdmCb.filter(F.a == AdmAct.DECIDE), staff)
    cb(on_fee_toggle, AdmCb.filter(F.a == AdmAct.FEE), staff)
    cb(on_split_custom, AdmCb.filter(F.a == AdmAct.SPLIT_CUSTOM), staff)
    cb(on_confirm_decision, AdmCb.filter(F.a == AdmAct.CONFIRM), staff)
    cb(on_message_parties, AdmCb.filter(F.a == AdmAct.MESSAGE), staff)
    msg(on_split_pct, AdminSG.split_pct, F.text, staff)
    msg(on_comment, AdminSG.comment, F.text, staff)
    msg(on_parties_text, AdminSG.message, F.text, staff)
    # money, users and overview: admins only
    cb(on_approvals, AdmCb.filter(F.a == AdmAct.APPROVALS), admins)
    cb(on_failed, AdmCb.filter(F.a == AdmAct.FAILED), admins)
    cb(on_payout, AdmCb.filter(F.a == AdmAct.PAYOUT), admins)
    cb(on_approve, AdmCb.filter(F.a == AdmAct.APPROVE), admins)
    cb(on_reject, AdmCb.filter(F.a == AdmAct.REJECT), admins)
    cb(on_retry, AdmCb.filter(F.a == AdmAct.RETRY), admins)
    cb(on_unidentified, AdmCb.filter(F.a == AdmAct.UNIDENTIFIED), admins)
    cb(on_transfer, AdmCb.filter(F.a == AdmAct.TRANSFER), admins)
    cb(on_assign, AdmCb.filter(F.a == AdmAct.ASSIGN), admins)
    cb(on_return, AdmCb.filter(F.a == AdmAct.RETURN), admins)
    cb(on_balances, AdmCb.filter(F.a == AdmAct.BALANCES), admins)
    cb(on_pause, AdmCb.filter(F.a == AdmAct.PAUSE), admins)
    cb(on_resume, AdmCb.filter(F.a == AdmAct.RESUME), admins)
    cb(on_reconcile, AdmCb.filter(F.a == AdmAct.RECONCILE), admins)
    cb(on_chains, AdmCb.filter(F.a == AdmAct.CHAINS), admins)
    cb(on_search, AdmCb.filter(F.a == AdmAct.SEARCH), admins)
    cb(on_deal, AdmCb.filter(F.a == AdmAct.DEAL), staff)
    cb(on_user, AdmCb.filter(F.a == AdmAct.USER), admins)
    cb(on_ban, AdmCb.filter(F.a == AdmAct.BAN), admins)
    cb(on_unban, AdmCb.filter(F.a == AdmAct.UNBAN), admins)
    msg(on_reject_reason, AdminSG.reject_reason, F.text, admins)
    msg(on_assign_code, AdminSG.assign_code, F.text, admins)
    msg(on_return_recipient, AdminSG.return_recipient, F.text, admins)
    msg(on_search_query, AdminSG.search, F.text, admins)
    msg(on_ban_reason, AdminSG.ban_reason, F.text, admins)
    return router
