"""Admin panel texts: menu, dispute dossier, payout and payment cards, balances."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.bot import texts as T
from app.bot.render import PartyInfo, money
from app.bot.texts import esc
from app.db.models import ChainCursor, Deal, DealEvent, DealMessage, IncomingTransfer, Payout, User
from app.domain.enums import DisputeReason, PartyRole
from app.domain.fees import Settlement
from app.domain.money import format_amount
from app.services.admin import AdminCounts, BalanceRow, DisputeDossier
from app.services.ledger import AccountCode

MESSAGE_LIMIT = 3900  # Telegram allows 4096; leave room for headers

EVENT_LABELS = {
    "create": "создана",
    "accept": "принята",
    "start_payment": "счёт выставлен",
    "fund": "оплачена",
    "seller_confirm": "продавец: выполнил",
    "buyer_confirm": "покупатель: получил",
    "start_payout": "выплата",
    "complete": "завершена",
    "request_cancel": "предложение отмены",
    "revoke_cancel": "отзыв отмены",
    "request_refund": "запрос возврата",
    "agree_refund": "согласие на возврат",
    "object_refund": "возражение",
    "open_dispute": "спор открыт",
    "withdraw_dispute": "спор отозван",
    "notify": "напоминание",
    "expire": "истекла",
    "cancel": "отменена",
    "edit": "изменена",
    "late_payment": "поздний платёж",
}
REASON = {
    DisputeReason.USER: "открыт стороной",
    DisputeReason.BUYER_TIMEOUT: "покупатель молчал 72 ч после «Я выполнил»",
    DisputeReason.REFUND_OBJECTION: "продавец возразил против возврата",
}


def render_menu(counts: AdminCounts) -> str:
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"⚖️ Открытые споры: <b>{counts.disputes}</b>\n"
        f"💸 Выплаты ждут подтверждения: <b>{counts.approvals}</b>\n"
        f"🚨 Проблемные выплаты: <b>{counts.failed_payouts}</b>\n"
        f"❓ Неопознанные платежи: <b>{counts.unidentified}</b>"
    )


def _who(users: dict[int, User], user_id: int | None) -> str:
    if user_id is None:
        return "—"
    user = users.get(user_id)
    return PartyInfo.from_user(user).mention() if user else f"id{user_id}"


def _party(users: dict[int, User], user_id: int | None, role: PartyRole) -> str:
    user = users.get(user_id) if user_id is not None else None
    if user is None:
        return f"{T.ROLE[role]}: —"
    info = PartyInfo.from_user(user)
    flags = " 🚫 заблокирован" if user.is_banned else ""
    return (
        f"{T.ROLE[role]}: {info.mention()} (id <code>{user.id}</code>) — "
        f"{T.deals_count(user.deals_completed)}, "
        f"споров: {user.deals_disputed}, проиграно: {user.disputes_lost}{flags}"
    )


def _message_line(m: DealMessage, users: dict[int, User]) -> str:
    who = {"buyer": "Покупатель", "seller": "Продавец", "arbitrator": "Арбитр"}.get(
        m.sender_role, m.sender_role
    )
    media = f"[{m.file_type}] " if m.file_type else ""
    flags = f" ⚠️{','.join(m.flagged)}" if m.flagged else ""
    text = esc((m.text or "")[:300])
    return f"<i>{m.created_at:%d.%m %H:%M}</i> <b>{who}</b>: {media}{text}{flags}"


def _event_line(e: DealEvent) -> str:
    label = EVENT_LABELS.get(e.action, e.action)
    actor = e.actor_type.value if e.actor_id is None else f"{e.actor_type.value} {e.actor_id}"
    tx = f" · tx <code>{esc(e.tx_hash[:24])}</code>" if e.tx_hash else ""
    return f"<i>{e.created_at:%d.%m %H:%M}</i> {label} ({actor}){tx}"


def render_dossier(d: DisputeDossier) -> str:
    deal, dispute = d.deal, d.dispute
    arbiter = f", арбитр {_who(d.users, dispute.arbitrator_id)}" if dispute.arbitrator_id else ""
    opener = _who(d.users, dispute.opened_by) if dispute.opened_by else "система"
    lines = [
        f"⚖️ <b>Спор по сделке <code>{esc(deal.code)}</code></b> · {dispute.status.value}{arbiter}",
        f"Открыт {dispute.created_at:%d.%m.%Y %H:%M} UTC · {REASON[dispute.reason]} ({opener})",
    ]
    if dispute.reason_text:
        lines.append(f"Причина: <blockquote>{esc(dispute.reason_text[:800])}</blockquote>")
    lines += [
        "",
        f"В escrow: <b>{money(d.escrow, deal.decimals)}</b> · сделка {money(deal.amount, deal.decimals)} · "
        f"{T.NETWORK_SHORT[deal.network]}",
        f"Продавцу по условиям: {money(deal.seller_receives, deal.decimals)} · "
        f"комиссия {money(deal.fee_amount, deal.decimals)}",
        _party(d.users, deal.buyer_id, PartyRole.BUYER),
        _party(d.users, deal.seller_id, PartyRole.SELLER),
        "",
        f"Условия:\n<blockquote expandable>{esc(deal.terms[:1500])}</blockquote>",
    ]
    if d.transfers:
        lines.append("Платежи:")
        lines += [
            f"• {format_amount(t.amount, deal.decimals)} USDT · <code>{esc(t.tx_hash[:40])}</code> · "
            f"{t.first_seen_at:%d.%m %H:%M}"
            for t in d.transfers
        ]
    if d.payouts:
        lines.append("Выплаты:")
        lines += [
            f"• {p.kind.value} {format_amount(p.amount, deal.decimals)} USDT · {p.status.value}"
            for p in d.payouts
        ]
    lines.append("")
    lines.append("Хронология:")
    lines += [_event_line(e) for e in d.events]
    head = "\n".join(lines)
    chat = [_message_line(m, d.users) for m in d.messages]
    body = "\n".join(["", f"Чат сделки ({len(chat)} последних):", *chat]) if chat else "\nЧат сделки: пусто"
    text = head + "\n" + body
    if len(text) > MESSAGE_LIMIT:
        text = head[: MESSAGE_LIMIT - 200] + "\n…\n(полная переписка — кнопкой «Весь чат»)"
    return text


def render_resolution_preview(
    deal: Deal, *, decision: str, bps: int, keep_fee: bool, escrow: int, s: Settlement
) -> str:
    title = {
        "buyer": "всё покупателю",
        "seller": "всё продавцу",
        "split": f"разделить: покупателю {bps / 100:g}%",
    }
    return "\n".join(
        [
            f"⚖️ <b>Решение по сделке <code>{esc(deal.code)}</code>: {title[decision]}</b>",
            "",
            f"В escrow: {money(escrow, deal.decimals)}",
            f"Покупателю: <b>{money(s.to_buyer, deal.decimals)}</b>",
            f"Продавцу: <b>{money(s.to_seller, deal.decimals)}</b>",
            f"Комиссия сервиса: {money(s.fee_revenue, deal.decimals)} "
            f"({'удерживается' if keep_fee else 'не берётся'})",
            f"Сетевые комиссии выплат: {money(s.network_fee_recovered, deal.decimals)}",
            "",
            "После подтверждения нужен комментарий (его увидят стороны) и код TOTP.",
        ]
    )


def render_payout(payout: Payout, deal: Deal | None, users: dict[int, User]) -> str:
    decimals = deal.decimals if deal else 6
    recipient = (
        _who(users, payout.recipient_user_id)
        if payout.recipient_user_id
        else f"<code>{esc(payout.recipient_address or '—')}</code>"
    )
    lines = [
        f"💸 <b>Выплата #{payout.id}</b> · {payout.kind.value} · {payout.status.value}",
        f"Сумма: <b>{format_amount(payout.amount, decimals)} USDT</b> · {T.NETWORK_SHORT[payout.network]}",
        f"Получатель: {recipient}",
        f"Сделка: <code>{esc(deal.code)}</code> · {T.STATUS[deal.status]}" if deal else "Без сделки",
        f"Попыток: {payout.attempts}",
    ]
    if payout.last_error:
        lines.append(f"Последняя ошибка: <code>{esc(payout.last_error[:300])}</code>")
    return "\n".join(lines)


def render_transfer(t: IncomingTransfer, candidates: Sequence[Deal], decimals: int) -> str:
    lines = [
        f"❓ <b>Платёж #{t.id}</b> · {T.NETWORK_SHORT[t.network]}",
        f"Сумма: <b>{format_amount(t.amount, decimals)} USDT</b>",
        f"ID / tx: <code>{esc(t.tx_hash)}</code>",
        f"Отправитель: <code>{esc(t.from_address or '—')}</code>",
        f"Получен: {t.first_seen_at:%d.%m.%Y %H:%M} UTC",
    ]
    problems = (t.raw or {}).get("problems") if isinstance(t.raw, dict) else None
    if problems:
        lines.append(f"Проблемы: {esc(str(problems))}")
    if candidates:
        lines.append("\nПохожие сделки:")
        lines += [
            f"• <code>{esc(d.code)}</code> · к оплате {format_amount(d.buyer_pays, d.decimals)} · "
            f"{T.STATUS[d.status]}"
            for d in candidates
        ]
    lines.append("\nПривязать к сделке или вернуть отправителю (нужен TOTP).")
    return "\n".join(lines)


def render_balances(
    rows: Sequence[BalanceRow],
    cryptobot: dict[str, tuple[str, str]] | None,
    decimals_for: Callable[[str], int],
) -> str:
    if not rows:
        return "💰 Движений по ledger ещё нет."
    lines = ["💰 <b>Балансы (ledger)</b>"]
    for row in rows:
        decimals = decimals_for(row.asset)

        def f(code: AccountCode, row: BalanceRow = row, decimals: int = decimals) -> str:
            return format_amount(row.get(code), decimals)

        lines += [
            "",
            f"<b>{esc(row.asset)}</b>",
            *[
                f"• кошелёк {esc(wallet)}: {format_amount(value, decimals)}"
                for wallet, value in row.custody.items()
            ],
            f"• в escrow: {f(AccountCode.ESCROW)} · к выплате: {f(AccountCode.PAYABLE)}",
            f"• неопознанные: {f(AccountCode.UNIDENTIFIED)} · "
            f"к возврату: {f(AccountCode.PAYABLE_UNIDENTIFIED)}",
            f"• доход: комиссии {f(AccountCode.FEE_REVENUE)} + сеть {f(AccountCode.NETWORK_FEE_RECOVERED)}",
            f"• расходы: провайдер {f(AccountCode.PROVIDER_FEE_EXPENSE)} · "
            f"газ {f(AccountCode.NETWORK_FEE_EXPENSE)}",
        ]
        if row.asset == "USDT@cryptobot" and cryptobot and "USDT" in cryptobot:
            available, onhold = cryptobot["USDT"]
            lines.append(f"• CryptoBot API: доступно {esc(available)}, в холде {esc(onhold)}")
    return "\n".join(lines)


def render_chains(cursors: Sequence[ChainCursor]) -> str:
    if not cursors:
        return "🛰 Ончейн-сети ещё не подключены (ChainWatcher — следующий этап)."
    lines = ["🛰 <b>ChainWatcher</b>"]
    for c in cursors:
        lag = (c.head_latest or 0) - int(c.cursor_seen or 0) if (c.cursor_seen or "").isdigit() else None
        lines.append(
            f"• {c.network.value}/{esc(c.stream)}: head {c.head_latest}, final {c.head_final}, "
            f"отставание {lag if lag is not None else '—'}, RPC {esc(c.active_endpoint or '—')}"
        )
        if c.last_error:
            lines.append(f"  ошибка: <code>{esc(c.last_error[:200])}</code>")
    return "\n".join(lines)


def render_user(user: User, deals: Sequence[Deal]) -> str:
    info = PartyInfo.from_user(user)
    lines = [
        f"👤 <b>{info.mention()}</b> · id <code>{user.id}</code> · роль {user.role.value}",
        f"Сделок завершено: {user.deals_completed}, споров: {user.deals_disputed}, "
        f"проиграно: {user.disputes_lost}",
        f"Зарегистрирован: {user.created_at:%d.%m.%Y}",
    ]
    if user.is_banned:
        lines.append(f"🚫 Заблокирован: {esc(user.ban_reason or '—')}")
    if deals:
        lines.append("\nПоследние сделки:")
        lines += [
            f"• <code>{esc(d.code)}</code> · {format_amount(d.amount, d.decimals)} USDT · "
            f"{T.STATUS[d.status]}"
            for d in deals
        ]
    return "\n".join(lines)
