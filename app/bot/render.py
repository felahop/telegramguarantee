"""Rendering of deal cards, draft previews and deal lists (HTML)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.bot import texts as T
from app.bot.drafts import DealDraft
from app.bot.texts import esc
from app.config import AssetSpec
from app.db.models import Deal, PaymentRequest, User
from app.domain.enums import DealStatus, FeePayer, NetworkFeePayer, PartyRole, Provider
from app.domain.fees import FeeBreakdown
from app.domain.money import format_amount

USERNAME_CHANGE_WARNING = timedelta(days=14)
TERMS_PREVIEW_LIMIT = 900


@dataclass(frozen=True)
class PartyInfo:
    user_id: int
    username: str | None
    first_name: str
    deals_completed: int
    disputes_lost: int
    username_changed_at: datetime | None

    @classmethod
    def from_user(cls, user: User) -> PartyInfo:
        return cls(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            deals_completed=user.deals_completed,
            disputes_lost=user.disputes_lost,
            username_changed_at=user.username_changed_at,
        )

    def mention(self) -> str:
        return f"@{esc(self.username)}" if self.username else esc(self.first_name or f"id{self.user_id}")


def money(amount: int, spec_or_decimals: AssetSpec | int) -> str:
    decimals = spec_or_decimals if isinstance(spec_or_decimals, int) else spec_or_decimals.decimals
    return f"{format_amount(amount, decimals)} USDT"


def fee_rate(bps: int) -> str:
    return f"{(Decimal(bps) / 100).normalize():f}%"


def party_line(role: PartyRole, info: PartyInfo | None, *, now: datetime, placeholder: str) -> str:
    if info is None:
        return f"{T.ROLE[role]}: {placeholder}"
    if info.deals_completed:
        stats = f"{T.deals_count(info.deals_completed)}, проиграно споров: {info.disputes_lost}"
    else:
        stats = "новый пользователь ⚠️"
    line = f"{T.ROLE[role]}: {info.mention()} — {stats}"
    if info.username_changed_at and now - info.username_changed_at < USERNAME_CHANGE_WARNING:
        days = max((now - info.username_changed_at).days, 0)
        line += f"\n   ⚠️ сменил username {days} дн. назад"
    return line


def _amount_lines(
    *,
    amount: int,
    fee: int,
    fee_bps: int,
    fee_payer: FeePayer,
    network_fee: int,
    network_fee_payer: NetworkFeePayer,
    provider: Provider,
    buyer_pays: int,
    seller_receives: int,
    decimals: int,
) -> list[str]:
    lines = [
        f"Сумма сделки: <b>{money(amount, decimals)}</b>",
        f"Комиссия сервиса {fee_rate(fee_bps)}: {money(fee, decimals)} ({T.FEE_PAYER[fee_payer]})",
    ]
    if provider is Provider.ONCHAIN:
        if network_fee:
            lines.append(
                f"Сетевая комиссия выплаты: {money(network_fee, decimals)} "
                f"({T.NETWORK_FEE_PAYER[network_fee_payer]})"
            )
        else:
            lines.append("Сетевая комиссия выплаты: за счёт сервиса")
    lines += [
        f"К оплате: <b>{money(buyer_pays, decimals)}</b>",
        f"Продавец получит: <b>{money(seller_receives, decimals)}</b>",
    ]
    return lines


def _terms_block(terms: str) -> str:
    shown = terms if len(terms) <= TERMS_PREVIEW_LIMIT else terms[:TERMS_PREVIEW_LIMIT] + "…"
    return f"Условия:\n<blockquote expandable>{esc(shown)}</blockquote>"


def render_deal_card(
    deal: Deal,
    *,
    viewer_id: int,
    parties: dict[int, PartyInfo],
    spec: AssetSpec,
    now: datetime,
    payment: PaymentRequest | None = None,
    refund_deadline: datetime | None = None,
    needs_refund_address: bool = False,
) -> str:
    def info(user_id: int | None) -> PartyInfo | None:
        return parties.get(user_id) if user_id is not None else None

    awaiting = "ожидает принятия"
    if deal.invited_username:
        awaiting = f"ожидает принятия (@{esc(deal.invited_username)})"
    elif deal.invited_user_id and deal.invited_user_id in parties:
        awaiting = f"ожидает принятия ({parties[deal.invited_user_id].mention()})"
    elif not deal.invited_user_id:
        awaiting = "по ссылке-приглашению"

    lines = [
        f"🛡 Сделка <code>{esc(deal.code)}</code> · {T.STATUS[deal.status]}",
        "",
        party_line(PartyRole.BUYER, info(deal.buyer_id), now=now, placeholder=awaiting),
        party_line(PartyRole.SELLER, info(deal.seller_id), now=now, placeholder=awaiting),
        "",
        *_amount_lines(
            amount=deal.amount,
            fee=deal.fee_amount,
            fee_bps=deal.fee_rate_bps,
            fee_payer=deal.fee_payer,
            network_fee=deal.network_fee_amount,
            network_fee_payer=deal.network_fee_payer,
            provider=deal.provider,
            buyer_pays=deal.buyer_pays,
            seller_receives=deal.seller_receives,
            decimals=deal.decimals,
        ),
        "",
        f"Сеть: {esc(spec.title)} · Срок выполнения: {T.hours_label(deal.execution_hours)} после оплаты",
    ]
    if deal.execution_deadline_at and deal.status in (DealStatus.FUNDED, DealStatus.SELLER_CONFIRMED):
        lines.append(f"Дедлайн: {deal.execution_deadline_at:%d.%m.%Y %H:%M} UTC")
    if viewer_id == deal.seller_id and deal.seller_payout_address:
        lines.append(f"Адрес выплаты: <code>{esc(deal.seller_payout_address)}</code>")
    lines += _payment_lines(deal, payment, viewer_id)
    lines += _progress_lines(
        deal, viewer_id, refund_deadline=refund_deadline, needs_refund_address=needs_refund_address
    )
    lines += ["", _terms_block(deal.terms)]

    role = deal.party_role(viewer_id)
    if role is not None:
        lines += ["", f"<i>{T.ROLE_YOU[role]}.</i>"]
    elif viewer_id != deal.creator_id and deal.status is DealStatus.CREATED:
        lines += ["", f"<i>Вам предлагают стать стороной: {T.ROLE[deal.creator_role.other].lower()}.</i>"]
    if viewer_id == deal.creator_id and deal.pending_counterparty_id:
        candidate = parties.get(deal.pending_counterparty_id)
        who = candidate.mention() if candidate else f"id{deal.pending_counterparty_id}"
        lines += ["", f"❗ Принять сделку хочет {who}. Подтвердите, что это ваш контрагент."]
    if deal.version > 1 and deal.status is DealStatus.CREATED:
        lines += [f"<i>Условия изменены (версия {deal.version}).</i>"]
    lines += ["", T.SAFETY_FOOTER]
    return "\n".join(lines)


def _when(moment: datetime | None) -> str:
    return f"{moment:%d.%m %H:%M} UTC" if moment else "—"


def _progress_lines(  # noqa: PLR0912 - one hint per status and role
    deal: Deal, viewer_id: int, *, refund_deadline: datetime | None, needs_refund_address: bool
) -> list[str]:
    """What happens now and what the viewer can do (shown under the amounts)."""
    role = deal.party_role(viewer_id)
    if role is None:
        return []
    buyer = role is PartyRole.BUYER
    status = deal.status
    lines: list[str] = []
    if status is DealStatus.FUNDED:
        if buyer:
            lines.append(
                "🔒 Деньги у гаранта. Продавец выполняет обязательства; без вашего подтверждения "
                "деньги ему не уйдут."
            )
        else:
            lines.append(
                f"🔒 Оплата получена. Выполните обязательства до {_when(deal.execution_deadline_at)} "
                "и нажмите «Я выполнил»."
            )
    elif status is DealStatus.SELLER_CONFIRMED:
        if buyer:
            lines.append(
                "📦 Продавец сообщил о выполнении. Проверьте и подтвердите получение — только после "
                "этого деньги уйдут продавцу. Если что-то не так — откройте спор."
            )
        else:
            lines.append("⏳ Ждём подтверждения покупателя. Если он не ответит 72 ч — откроется спор.")
    elif status is DealStatus.DISPUTED:
        lines.append(
            "⚖️ Идёт спор: деньги заморожены до решения арбитра. Пишите в чат сделки — арбитр "
            "увидит переписку."
        )
    elif status is DealStatus.PAYOUT_PENDING:
        lines.append("💸 Выплата продавцу отправляется.")
    elif status is DealStatus.REFUND_PENDING:
        lines.append("↩️ Деньги возвращаются покупателю.")
        if buyer and needs_refund_address:
            lines.append("📮 Укажите адрес для возврата — без него бот не может отправить деньги.")
    if status in (DealStatus.FUNDED, DealStatus.SELLER_CONFIRMED):
        mine = deal.cancel_req_buyer_at if buyer else deal.cancel_req_seller_at
        theirs = deal.cancel_req_seller_at if buyer else deal.cancel_req_buyer_at
        if mine is not None:
            lines.append("↩️ Вы предложили отменить сделку — ждём согласия второй стороны.")
        elif theirs is not None:
            lines.append(
                "↩️ Вторая сторона предлагает отменить сделку. Если согласитесь — деньги вернутся покупателю."
            )
    if status is DealStatus.FUNDED and deal.refund_requested_at is not None:
        if buyer:
            lines.append(
                f"↩️ Вы запросили возврат. Если продавец не возразит до {_when(refund_deadline)}, "
                "деньги вернутся вам."
            )
        else:
            lines.append(
                f"↩️ Покупатель запросил возврат: срок истёк. До {_when(refund_deadline)} согласитесь "
                "или возразите — иначе деньги вернутся покупателю."
            )
    return ["", *lines] if lines else []


def _payment_lines(deal: Deal, payment: PaymentRequest | None, viewer_id: int) -> list[str]:
    if deal.status is not DealStatus.AWAITING_PAYMENT or payment is None:
        return []
    until = f"{payment.expires_at:%H:%M} UTC"
    if viewer_id != deal.buyer_id:
        return ["", f"⏳ Ждём оплату покупателя до {until}. Не передавайте товар до статуса «Оплачено»."]
    lines = ["", f"💳 Оплатите <b>{money(payment.expected_amount, deal.decimals)}</b> до {until}."]
    if deal.provider is Provider.CRYPTOBOT:
        lines.append("Оплата проходит в @CryptoBot по кнопке ниже — бот увидит её автоматически.")
    return lines


def render_draft_preview(
    draft: DealDraft,
    breakdown: FeeBreakdown,
    spec: AssetSpec,
    *,
    fee_bps: int,
    network_fee_payer: NetworkFeePayer,
) -> str:
    if (
        draft.role is None
        or draft.network is None
        or draft.fee_payer is None
        or draft.terms is None
        or draft.hours is None
    ):
        raise ValueError("draft is incomplete")
    if draft.counterparty_label:
        counterparty = esc(draft.counterparty_label)
    elif draft.counterparty_username:
        counterparty = f"@{esc(draft.counterparty_username)}"
    else:
        counterparty = "по ссылке-приглашению"
    title = "✏️ Изменение сделки" if draft.editing else "👀 Проверьте сделку"
    lines = [
        f"<b>{title}</b>",
        "",
        f"{T.ROLE_YOU[draft.role]}. Вторая сторона: {counterparty}",
        "",
        *_amount_lines(
            amount=breakdown.amount,
            fee=breakdown.fee,
            fee_bps=fee_bps,
            fee_payer=draft.fee_payer,
            network_fee=breakdown.network_fee,
            network_fee_payer=network_fee_payer,
            provider=draft.network.provider,
            buyer_pays=breakdown.buyer_pays,
            seller_receives=breakdown.seller_receives,
            decimals=spec.decimals,
        ),
        "",
        f"Сеть: {esc(spec.title)} · Срок выполнения: {T.hours_label(draft.hours)} после оплаты",
    ]
    if draft.payout_address:
        lines.append(f"Адрес выплаты: <code>{esc(draft.payout_address)}</code>")
    lines += ["", _terms_block(draft.terms)]
    if draft.editing:
        lines += ["", "<i>После изменения вторая сторона должна будет принять условия заново.</i>"]
    return "\n".join(lines)


def deal_list_line(deal: Deal, viewer_id: int) -> str:
    role = deal.party_role(viewer_id) or deal.creator_role
    arrow = "🛒" if role is PartyRole.BUYER else "💼"
    return f"{arrow} {deal.code} · {format_amount(deal.amount, deal.decimals)} USDT · {T.STATUS[deal.status]}"


def render_inline_card(
    *,
    creator: User,
    role: PartyRole,
    counterparty_username: str | None,
    breakdown: FeeBreakdown,
    spec: AssetSpec,
    fee_payer: FeePayer,
    fee_bps: int,
    hours: int,
    terms: str,
    network_fee_payer: NetworkFeePayer,
    bot_username: str,
) -> str:
    me = PartyInfo.from_user(creator).mention()
    other = f"@{esc(counterparty_username)}" if counterparty_username else "тот, кто откроет сделку"
    buyer, seller = (me, other) if role is PartyRole.BUYER else (other, me)
    lines = [
        f"🛡 <b>Сделка через гаранта @{esc(bot_username)}</b>",
        "",
        f"Покупатель: {buyer}",
        f"Продавец: {seller}",
        "",
        *_amount_lines(
            amount=breakdown.amount,
            fee=breakdown.fee,
            fee_bps=fee_bps,
            fee_payer=fee_payer,
            network_fee=breakdown.network_fee,
            network_fee_payer=network_fee_payer,
            provider=spec.network.provider,
            buyer_pays=breakdown.buyer_pays,
            seller_receives=breakdown.seller_receives,
            decimals=spec.decimals,
        ),
        "",
        f"Сеть: {esc(spec.title)} · Срок выполнения: {T.hours_label(hours)} после оплаты",
        "",
        _terms_block(terms),
        "",
        "Откройте сделку в боте кнопкой ниже, чтобы принять условия. Деньги держит гарант, продавец получит "
        "их только после подтверждения покупателя.",
    ]
    return "\n".join(lines)
