"""Rendering of outbox notifications for their recipient.

The card is rendered from the deal's *current* state at sending time, so the buttons are always
the ones valid now (a notice delivered late never offers an action that is no longer possible).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts as T
from app.bot.admin_keyboards import AdmAct, AdmCb
from app.bot.context import BotContext
from app.bot.render import PartyInfo
from app.bot.texts import esc
from app.bot.views import deal_view
from app.db.models import Deal, NotificationOutbox, User
from app.domain.enums import DisputeDecision, DisputeReason, PartyRole
from app.domain.money import format_amount
from app.services.notifications import ADMIN_NOTICES, Notice


@dataclass(frozen=True)
class RenderedNotice:
    text: str
    markup: InlineKeyboardMarkup | None = None
    inline_message_id: str | None = None  # edit an inline-mode message instead of sending


def _amount(deal: Deal, payload: dict[str, Any], key: str = "amount") -> str:
    value = payload.get(key)
    return f"{format_amount(int(value), deal.decimals)} USDT" if value is not None else ""


def _role(deal: Deal, user_id: int) -> PartyRole | None:
    return deal.party_role(user_id)


Headline = Callable[[Deal, int, dict[str, Any], dict[int, User]], str]


def _who(users: dict[int, User], user_id: object) -> str:
    if isinstance(user_id, int) and user_id in users:
        return PartyInfo.from_user(users[user_id]).mention()
    return "пользователь"


def _funded(deal: Deal, uid: int, p: dict[str, Any], _: dict[int, User]) -> str:
    if _role(deal, uid) is PartyRole.SELLER:
        return (
            "🔒 <b>Оплата получена — деньги у гаранта.</b> Выполните обязательства и нажмите "
            "«Я выполнил». Не передавайте товар, пока бот не подтвердил оплату."
        )
    extra = ""
    if p.get("overpaid_to_seller"):
        excess = _amount(deal, p, "overpaid_to_seller")
        extra = f"\nПереплата {excess} меньше стоимости возврата и будет передана продавцу."
    if p.get("underpayment_accepted"):
        extra = "\nПродавец согласился работать с фактически полученной суммой."
    return f"🔒 <b>Оплата получена, деньги у гаранта.</b> Продавец выполняет обязательства.{extra}"


def _resolved(deal: Deal, uid: int, p: dict[str, Any], _: dict[int, User]) -> str:
    decision = DisputeDecision(p.get("decision", "split"))
    outcome = {
        DisputeDecision.BUYER: "в пользу покупателя",
        DisputeDecision.SELLER: "в пользу продавца",
        DisputeDecision.SPLIT: "разделить сумму",
    }[decision]
    lines = [f"⚖️ <b>Спор решён: {outcome}.</b>"]
    if p.get("to_buyer"):
        lines.append(f"Покупателю: {_amount(deal, p, 'to_buyer')}")
    if p.get("to_seller"):
        lines.append(f"Продавцу: {_amount(deal, p, 'to_seller')}")
    if p.get("comment"):
        lines.append(f"Комментарий арбитра: {esc(str(p['comment']))}")
    return "\n".join(lines)


def _dispute_opened(deal: Deal, uid: int, p: dict[str, Any], _: dict[int, User]) -> str:
    reason = p.get("reason")
    if reason == DisputeReason.BUYER_TIMEOUT.value:
        return (
            "⚖️ <b>Открыт спор:</b> покупатель не подтвердил получение вовремя. Деньги заморожены, "
            "арбитр свяжется со сторонами через бота."
        )
    if reason == DisputeReason.REFUND_OBJECTION.value:
        return (
            "⚖️ <b>Открыт спор:</b> продавец возражает против возврата. Деньги заморожены до решения арбитра."
        )
    return "⚖️ <b>Вторая сторона открыла спор.</b> Деньги заморожены до решения арбитра."


HEADLINES: dict[Notice, Headline] = {
    Notice.DEAL_OFFER: lambda d, u, p, us: (
        "📨 <b>Вам предложили сделку через гаранта.</b> Проверьте условия."
    ),
    Notice.DEAL_EDITED: lambda d, u, p, us: (
        "✏️ <b>Условия сделки изменены.</b> Проверьте и примите их заново."
    ),
    Notice.CONFIRM_COUNTERPARTY: lambda d, u, p, us: (
        f"❗ <b>Сделку хочет принять {_who(us, p.get('user_id'))}.</b> Подтвердите, что это ваш контрагент."
    ),
    Notice.COUNTERPARTY_REJECTED: lambda d, u, p, us: (
        "🚫 Создатель сделки не подтвердил вас как контрагента."
    ),
    Notice.DEAL_ACCEPTED: lambda d, u, p, us: (
        "🤝 <b>Условия приняты.</b> Покупатель может переходить к оплате."
        if _role(d, u) is PartyRole.SELLER
        else "🤝 <b>Условия приняты.</b> Можно переходить к оплате."
    ),
    Notice.PAYMENT_STARTED: lambda d, u, p, us: "💳 Покупатель перешёл к оплате. Ждём поступления.",
    Notice.DEAL_FUNDED: _funded,
    Notice.SELLER_CONFIRMED: lambda d, u, p, us: (
        "📦 <b>Продавец сообщает, что выполнил обязательства.</b> Проверьте и подтвердите получение — "
        "только после этого деньги уйдут продавцу."
    ),
    Notice.PAYOUT_STARTED: lambda d, u, p, us: (
        f"💸 <b>Покупатель подтвердил получение.</b> Выплата {_amount(d, p)} отправляется вам."
    ),
    Notice.DEAL_COMPLETED: lambda d, u, p, us: (
        "✅ <b>Сделка завершена.</b> Спасибо, что пользуетесь гарантом!"
    ),
    Notice.DEAL_CANCELLED: lambda d, u, p, us: "✖️ Сделка отменена второй стороной.",
    Notice.DEAL_EXPIRED: lambda d, u, p, us: "⌛ Сделка истекла: время на принятие или оплату вышло.",
    Notice.CANCEL_REQUESTED: lambda d, u, p, us: (
        "↩️ <b>Вторая сторона предлагает отменить сделку.</b> Если вы тоже согласны, деньги вернутся "
        "покупателю. Иначе сделка продолжается."
    ),
    Notice.CANCEL_REVOKED: lambda d, u, p, us: "Вторая сторона отозвала предложение отменить сделку.",
    Notice.REFUND_REQUESTED: lambda d, u, p, us: (
        f"↩️ <b>Покупатель просит возврат:</b> срок выполнения истёк. У вас есть "
        f"{p.get('objection_window_hours', 12)} ч, чтобы согласиться или возразить — после этого деньги "
        "вернутся покупателю. Если вы уже выполнили обязательства — нажмите «Я выполнил», откроется спор."
    ),
    Notice.REFUND_STARTED: lambda d, u, p, us: (
        f"↩️ <b>Деньги возвращаются покупателю</b>{': ' + _amount(d, p) if p.get('amount') else ''}."
    ),
    Notice.REFUND_ADDRESS_NEEDED: lambda d, u, p, us: (
        f"📮 <b>{'Напоминаем: укажите' if p.get('reminder') else 'Укажите'} адрес для возврата</b> "
        f"в сети {T.NETWORK_SHORT[d.network]}. Деньги ждут вас. "
        "Не указывайте адрес биржи без проверки: возврат придёт на него без мемо."
    ),
    Notice.DEAL_REFUNDED: lambda d, u, p, us: "↩️ Возврат по сделке выполнен.",
    Notice.DEADLINE_PASSED: lambda d, u, p, us: (
        "⏰ <b>Срок выполнения истёк</b>, а продавец не подтвердил выполнение. Вы можете открыть спор или "
        "запросить возврат. Деньги без вашего подтверждения продавцу не уйдут."
    ),
    Notice.BUYER_REMINDER: lambda d, u, p, us: (
        "⏳ <b>Продавец ждёт вашего подтверждения.</b> Если всё получено — подтвердите. Если есть проблема — "
        "откройте спор. Без ответа через 72 ч после выполнения будет открыт спор автоматически."
    ),
    Notice.PAYOUT_ADDRESS_CHANGED: lambda d, u, p, us: (
        "ℹ️ Продавец изменил адрес выплаты (до оплаты это разрешено)."
    ),
    Notice.DISPUTE_OPENED: _dispute_opened,
    Notice.DISPUTE_WITHDRAWN: lambda d, u, p, us: "Спор отозван, сделка продолжается.",
    Notice.DISPUTE_RESOLVED: _resolved,
    Notice.DISPUTE_IN_REVIEW: lambda d, u, p, us: (
        "⚖️ <b>Арбитр взял спор в работу.</b> Он может написать вам через бота; отвечайте в чате сделки — "
        "арбитр видит переписку."
    ),
    Notice.PAYOUT_ACTION_NEEDED: lambda d, u, p, us: (
        f"⚠️ <b>Не удаётся отправить выплату.</b> {esc(str(p.get('hint', 'Обратитесь в поддержку.')))}"
    ),
}

ADMIN_HEADLINES: dict[Notice, str] = {
    Notice.ADMIN_DISPUTE_OPENED: "⚖️ Новый спор",
    Notice.ADMIN_DISPUTE_SLA: "⏰ Спор ждёт решения дольше SLA",
    Notice.ADMIN_PAYOUT_APPROVAL: "💸 Крупная выплата ждёт подтверждения",
    Notice.ADMIN_PAYOUT_FAILED: "🚨 Выплата не прошла",
    Notice.ADMIN_PAYMENT_ANOMALY: "🚨 Подозрительный платёж",
    Notice.ADMIN_RECONCILIATION: "⚠️ Сверка: требует внимания",
}
RECONCILIATION_CRITICAL = "🚨 Сверка: критично"


async def render_notice(
    ctx: BotContext, session: AsyncSession, row: NotificationOutbox
) -> RenderedNotice | None:
    """None means the notice is obsolete (e.g. the deal is gone) and should be skipped."""
    kind = Notice(row.kind)
    payload: dict[str, Any] = row.payload or {}
    deal = await session.get(Deal, row.deal_id) if row.deal_id is not None else None

    if kind is Notice.INLINE_CARD:
        if deal is None or not payload.get("inline_message_id"):
            return None
        text = (
            f"🤝 Сделка <code>{esc(deal.code)}</code> принята в @{esc(ctx.bot_username)}. "
            f"{T.STATUS[deal.status]}."
        )
        return RenderedNotice(text=text, inline_message_id=str(payload["inline_message_id"]))

    if kind in ADMIN_NOTICES:
        headline = ADMIN_HEADLINES[kind]
        if kind is Notice.ADMIN_RECONCILIATION and payload.get("severity") == "critical":
            headline = RECONCILIATION_CRITICAL
        lines = [f"<b>{headline}</b>"]
        if deal is not None:
            lines.append(
                f"Сделка <code>{esc(deal.code)}</code> · {format_amount(deal.amount, deal.decimals)} USDT · "
                f"{T.NETWORK_SHORT[deal.network]} · {T.STATUS[deal.status]}"
            )
        if kind is Notice.ADMIN_RECONCILIATION:
            lines.append(esc(str(payload.get("message", ""))))
        else:
            details = {k: v for k, v in payload.items() if k not in ("status", "code")}
            if details:
                lines.append(esc(", ".join(f"{k}: {v}" for k, v in details.items())))
        return RenderedNotice(text="\n".join(lines), markup=_admin_button(kind, deal, payload))

    if deal is None:
        return None
    users = {u.id: u for u in [await session.get(User, i) for i in _mentioned(payload)] if u is not None}
    headline = HEADLINES[kind](deal, row.user_id, payload, users)
    text, markup = await deal_view(ctx, session, deal, row.user_id)
    return RenderedNotice(text=f"{headline}\n\n{text}", markup=markup)


def _admin_button(kind: Notice, deal: Deal | None, payload: dict[str, Any]) -> InlineKeyboardMarkup:
    if kind in (Notice.ADMIN_PAYOUT_APPROVAL, Notice.ADMIN_PAYOUT_FAILED) and payload.get("payout_id"):
        target = AdmCb(a=AdmAct.PAYOUT, id=int(payload["payout_id"]))
    elif kind in (Notice.ADMIN_DISPUTE_OPENED, Notice.ADMIN_DISPUTE_SLA) and deal is not None:
        target = AdmCb(a=AdmAct.DISPUTE, id=deal.id)
    elif kind is Notice.ADMIN_PAYMENT_ANOMALY:
        target = AdmCb(a=AdmAct.UNIDENTIFIED)
    elif kind is Notice.ADMIN_RECONCILIATION:
        target = _reconciliation_target(payload, deal)
    else:
        target = AdmCb(a=AdmAct.MENU)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛠 Открыть в админке", callback_data=target.pack())]]
    )


def _reconciliation_target(payload: dict[str, Any], deal: Deal | None) -> AdmCb:
    check = str(payload.get("check", ""))
    if payload.get("payout_id"):
        return AdmCb(a=AdmAct.PAYOUT, id=int(payload["payout_id"]))
    if check == "unidentified":
        return AdmCb(a=AdmAct.UNIDENTIFIED)
    if deal is not None:
        return AdmCb(a=AdmAct.DEAL, id=deal.id)
    if check in ("balance", "solvency") or check.startswith("ledger"):
        return AdmCb(a=AdmAct.BALANCES)
    return AdmCb(a=AdmAct.MENU)


def _mentioned(payload: dict[str, Any]) -> list[int]:
    value = payload.get("user_id")
    return [value] if isinstance(value, int) else []
