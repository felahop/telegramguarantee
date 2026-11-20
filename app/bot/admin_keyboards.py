"""Admin panel callback data and keyboards."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Deal, Dispute, IncomingTransfer, Payout, User
from app.domain.enums import DisputeStatus, PayoutStatus
from app.domain.money import format_amount

PAGE = 8


class AdmAct(StrEnum):
    MENU = "m"
    DISPUTES = "ds"
    DISPUTE = "d"
    TAKE = "tk"
    FULL_CHAT = "ch"
    DECIDE = "dc"  # x = buyer | seller | split
    SPLIT_CUSTOM = "spc"
    FEE = "fee"
    CONFIRM = "ok"
    MESSAGE = "msg"
    APPROVALS = "ap"
    FAILED = "fl"
    PAYOUT = "p"
    APPROVE = "pa"
    REJECT = "pr"
    RETRY = "rt"
    UNIDENTIFIED = "un"
    TRANSFER = "t"
    ASSIGN = "as"
    RETURN = "rr"
    BALANCES = "bl"
    SEARCH = "sr"
    DEAL = "dl"
    USER = "u"
    BAN = "bn"
    UNBAN = "ub"
    CHAINS = "cn"
    CANCEL = "x"


class AdmCb(CallbackData, prefix="a"):
    a: AdmAct
    id: int = 0
    x: str = ""
    p: int = 0


def _b(text: str, action: AdmAct, id: int = 0, x: str = "", p: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AdmCb(a=action, id=id, x=x, p=p).pack())


def _m(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def back() -> list[InlineKeyboardButton]:
    return [_b("⬅️ Админ-панель", AdmAct.MENU)]


def menu() -> InlineKeyboardMarkup:
    return _m(
        [
            [_b("⚖️ Споры", AdmAct.DISPUTES), _b("💸 На подтверждении", AdmAct.APPROVALS)],
            [_b("🚨 Проблемные выплаты", AdmAct.FAILED), _b("❓ Неопознанные", AdmAct.UNIDENTIFIED)],
            [_b("💰 Балансы", AdmAct.BALANCES), _b("🔎 Поиск", AdmAct.SEARCH), _b("🛰 Сети", AdmAct.CHAINS)],
        ]
    )


def _pager(action: AdmAct, page: int, has_more: bool) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(_b("⬅️", action, p=page - 1))
    if has_more:
        row.append(_b("➡️", action, p=page + 1))
    return row


def disputes_list(items: Sequence[tuple[Dispute, Deal]], page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            _b(
                f"{'🟡' if d.status is DisputeStatus.IN_REVIEW else '🔴'} {deal.code} · "
                f"{format_amount(deal.amount, deal.decimals)} USDT · {d.created_at:%d.%m %H:%M}",
                AdmAct.DISPUTE,
                deal.id,
            )
        ]
        for d, deal in items
    ]
    return _m([*rows, _pager(AdmAct.DISPUTES, page, has_more), back()])


def dossier(deal: Deal, dispute: Dispute) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if dispute.status in (DisputeStatus.OPEN, DisputeStatus.IN_REVIEW):
        if dispute.status is DisputeStatus.OPEN:
            rows.append([_b("🙋 Взять в работу", AdmAct.TAKE, deal.id)])
        rows += [
            [
                _b("↩️ Всё покупателю", AdmAct.DECIDE, deal.id, "buyer"),
                _b("💼 Всё продавцу", AdmAct.DECIDE, deal.id, "seller"),
            ],
            [
                _b("➗ Разделить 50/50", AdmAct.DECIDE, deal.id, "split", 5000),
                _b("✍️ Другой %", AdmAct.SPLIT_CUSTOM, deal.id),
            ],
            [
                _b("✉️ Написать сторонам", AdmAct.MESSAGE, deal.id),
                _b("💬 Весь чат", AdmAct.FULL_CHAT, deal.id),
            ],
        ]
    rows.append([_b("🔄 Обновить", AdmAct.DISPUTE, deal.id), _b("⬅️ К спорам", AdmAct.DISPUTES)])
    return _m(rows)


def resolution(deal: Deal, decision: str, bps: int, keep_fee: bool) -> InlineKeyboardMarkup:
    toggle = "💰 Комиссия: удерживается" if keep_fee else "🆓 Комиссия: не берётся"
    return _m(
        [
            [_b("✅ Подтвердить решение", AdmAct.CONFIRM, deal.id, f"{decision}.{int(keep_fee)}", bps)],
            [_b(toggle, AdmAct.FEE, deal.id, f"{decision}.{int(not keep_fee)}", bps)],
            [_b("⬅️ К досье", AdmAct.DISPUTE, deal.id)],
        ]
    )


def payouts_list(items: Sequence[Payout], action: AdmAct, page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = [[_b(f"#{p.id} · {p.kind.value} · {p.status.value}", AdmAct.PAYOUT, p.id)] for p in items]
    return _m([*rows, _pager(action, page, has_more), back()])


def payout(p: Payout) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if p.status is PayoutStatus.AWAITING_APPROVAL:
        rows.append([_b("✅ Подтвердить", AdmAct.APPROVE, p.id), _b("⏸ Отклонить", AdmAct.REJECT, p.id)])
    if p.status in (PayoutStatus.FAILED_RETRYABLE, PayoutStatus.FAILED_FINAL):
        rows.append([_b("🔁 Повторить сейчас", AdmAct.RETRY, p.id)])
    if p.deal_id:
        rows.append([_b("🛡 Сделка", AdmAct.DEAL, p.deal_id)])
    rows.append(back())
    return _m(rows)


def transfers_list(items: Sequence[IncomingTransfer], page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = [
        [_b(f"#{t.id} · {t.network.value} · {t.first_seen_at:%d.%m %H:%M}", AdmAct.TRANSFER, t.id)]
        for t in items
    ]
    return _m([*rows, _pager(AdmAct.UNIDENTIFIED, page, has_more), back()])


def transfer(t: IncomingTransfer) -> InlineKeyboardMarkup:
    return _m(
        [
            [_b("🔗 Привязать к сделке", AdmAct.ASSIGN, t.id), _b("↩️ Вернуть", AdmAct.RETURN, t.id)],
            [_b("⬅️ К неопознанным", AdmAct.UNIDENTIFIED)],
        ]
    )


def search_results(deals: Sequence[Deal], users: Sequence[User]) -> InlineKeyboardMarkup:
    rows = [
        [_b(f"🛡 {d.code} · {format_amount(d.amount, d.decimals)} USDT · {d.status.value}", AdmAct.DEAL, d.id)]
        for d in deals[:10]
    ]
    rows += [[_b(f"👤 @{u.username or u.id}", AdmAct.USER, u.id)] for u in users[:5]]
    return _m([*rows, [_b("🔎 Новый поиск", AdmAct.SEARCH)], back()])


def user(u: User) -> InlineKeyboardMarkup:
    action = (
        _b("✅ Разблокировать", AdmAct.UNBAN, u.id)
        if u.is_banned
        else _b("🚫 Заблокировать", AdmAct.BAN, u.id)
    )
    return _m([[action], back()])


def deal_admin(deal: Deal, has_dispute: bool) -> InlineKeyboardMarkup:
    rows = [[_b("⚖️ Досье спора", AdmAct.DISPUTE, deal.id)]] if has_dispute else []
    return _m([*rows, back()])


def cancel() -> InlineKeyboardMarkup:
    return _m([[_b("✖️ Отмена", AdmAct.CANCEL)]])


def only_back() -> InlineKeyboardMarkup:
    return _m([back()])
