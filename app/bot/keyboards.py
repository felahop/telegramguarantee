"""Keyboards. Builders are pure functions of state, so they are unit-tested directly."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from urllib.parse import quote

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
)

from app.bot import texts as T
from app.bot.callbacks import DealAct, DealCb, MenuAct, MenuCb, TplAct, TplCb, WizAct, WizCb
from app.bot.drafts import DealDraft, Step
from app.config import AssetSpec
from app.db.models import Deal, DealTemplate, SavedAddress
from app.domain.enums import TERMINAL_STATUSES, DealStatus, FeePayer, Network, PartyRole
from app.domain.fees import FeeBreakdown
from app.domain.money import format_amount

QUICK_AMOUNTS = ("10", "50", "100", "500")
QUICK_HOURS = (1, 24, 72)
CONTACT_REQUEST_ID = 1


def _btn(text: str, cb: DealCb | WizCb | MenuCb | TplCb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def _markup(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def invite_url(bot_username: str, deal: Deal) -> str:
    return f"https://t.me/{bot_username}?start=j_{deal.invite_token}"


def share_url(url: str, text: str) -> str:
    return f"https://t.me/share/url?url={quote(url, safe='')}&text={quote(text, safe='')}"


# ── menu ─────────────────────────────────────────────────────────────────────


def main_menu() -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("➕ Новая сделка", MenuCb(a=MenuAct.NEW))],
            [_btn("📋 Мои сделки", MenuCb(a=MenuAct.DEALS)), _btn("📑 Шаблоны", MenuCb(a=MenuAct.TEMPLATES))],
            [_btn("❓ Как это работает", MenuCb(a=MenuAct.HELP))],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return _markup([[_btn("⬅️ В меню", MenuCb(a=MenuAct.MAIN))]])


# ── wizard ───────────────────────────────────────────────────────────────────


def _nav(draft: DealDraft, step: Step) -> list[InlineKeyboardButton]:
    row = []
    if step is not Step.ROLE and not (draft.editing and step is Step.AMOUNT):
        row.append(_btn("⬅️ Назад", WizCb(a=WizAct.BACK, val=step.value)))
    row.append(_btn("✖️ Отмена", WizCb(a=WizAct.CANCEL)))
    return row


def _keep(draft: DealDraft, step: Step, filled: bool) -> list[InlineKeyboardButton]:
    if draft.editing and filled:
        return [_btn("✔️ Оставить как есть", WizCb(a=WizAct.KEEP, val=step.value))]
    return []


def role_kb(draft: DealDraft) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                _btn("🛒 Я покупатель", WizCb(a=WizAct.ROLE, val=PartyRole.BUYER.value)),
                _btn("💼 Я продавец", WizCb(a=WizAct.ROLE, val=PartyRole.SELLER.value)),
            ],
            _nav(draft, Step.ROLE),
        ]
    )


def network_kb(draft: DealDraft, specs: Sequence[AssetSpec]) -> InlineKeyboardMarkup:
    rows = []
    for spec in specs:
        fee = f" · сеть ~{format_amount(spec.network_fee, spec.decimals)}" if spec.network_fee else ""
        mark = "• " if spec.network is draft.network else ""
        rows.append([_btn(f"{mark}{spec.title}{fee}", WizCb(a=WizAct.NETWORK, val=spec.network.value))])
    rows.append(_nav(draft, Step.NETWORK))
    return _markup(rows)


def amount_kb(draft: DealDraft, recent: Sequence[Decimal]) -> InlineKeyboardMarkup:
    values: list[str] = []
    for amount in [*map(str, recent), *QUICK_AMOUNTS]:
        normalized = f"{Decimal(amount).normalize():f}"
        if normalized not in values:
            values.append(normalized)
    buttons = [_btn(v, WizCb(a=WizAct.AMOUNT, val=v)) for v in values[:7]]
    return _markup(
        [
            buttons[:4],
            buttons[4:],
            _keep(draft, Step.AMOUNT, draft.amount is not None),
            _nav(draft, Step.AMOUNT),
        ]
    )


def terms_kb(draft: DealDraft) -> InlineKeyboardMarkup:
    return _markup([_keep(draft, Step.TERMS, draft.terms is not None), _nav(draft, Step.TERMS)])


def hours_kb(draft: DealDraft) -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn(T.hours_label(h), WizCb(a=WizAct.HOURS, val=str(h))) for h in QUICK_HOURS]
            + [_btn("✍️ Своё", WizCb(a=WizAct.HOURS_CUSTOM))],
            _keep(draft, Step.HOURS, draft.hours is not None),
            _nav(draft, Step.HOURS),
        ]
    )


def custom_hours_kb() -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                _btn("⬅️ Назад", WizCb(a=WizAct.GOTO, val=Step.HOURS.value)),
                _btn("✖️ Отмена", WizCb(a=WizAct.CANCEL)),
            ],
        ]
    )


def fee_kb(draft: DealDraft, quotes: dict[FeePayer, FeeBreakdown], spec: AssetSpec) -> InlineKeyboardMarkup:
    rows = []
    for payer, b in quotes.items():
        label = (
            f"{T.FEE_PAYER_BUTTON[payer]}: к оплате {format_amount(b.buyer_pays, spec.decimals)}, "
            f"продавцу {format_amount(b.seller_receives, spec.decimals)}"
        )
        rows.append([_btn(label, WizCb(a=WizAct.FEE, val=payer.value))])
    rows += [_keep(draft, Step.FEE, draft.fee_payer is not None), _nav(draft, Step.FEE)]
    return _markup(rows)


def address_kb(draft: DealDraft, saved: Sequence[SavedAddress]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"📌 {_short(a.address)}", WizCb(a=WizAct.ADDRESS, val=str(a.id)))] for a in saved]
    rows += [_keep(draft, Step.ADDRESS, draft.payout_address is not None), _nav(draft, Step.ADDRESS)]
    return _markup(rows)


def accept_address_kb(saved: Sequence[SavedAddress]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"📌 {_short(a.address)}", WizCb(a=WizAct.ADDRESS, val=str(a.id)))] for a in saved]
    rows.append([_btn("✖️ Отмена", WizCb(a=WizAct.CANCEL))])
    return _markup(rows)


def counterparty_kb(draft: DealDraft, recent: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"🔁 {label}", WizCb(a=WizAct.COUNTERPARTY, val=str(uid)))] for uid, label in recent]
    rows += [
        [_btn("👤 Выбрать из контактов", WizCb(a=WizAct.CONTACT))],
        [_btn("🔗 Получить ссылку-приглашение", WizCb(a=WizAct.OPEN_LINK))],
        _nav(draft, Step.COUNTERPARTY),
    ]
    return _markup(rows)


def contact_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="👤 Выбрать контакт",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=CONTACT_REQUEST_ID,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def preview_kb(draft: DealDraft) -> InlineKeyboardMarkup:
    action = "💾 Сохранить изменения" if draft.editing else "✅ Создать сделку"
    return _markup(
        [
            [_btn(action, WizCb(a=WizAct.CREATE))],
            [_btn("✏️ Изменить", WizCb(a=WizAct.FIELDS)), _btn("✖️ Отмена", WizCb(a=WizAct.CANCEL))],
        ]
    )


def fields_kb(draft: DealDraft, steps: Sequence[Step]) -> InlineKeyboardMarkup:
    labels = {
        Step.ROLE: "Роль",
        Step.NETWORK: "Сеть",
        Step.AMOUNT: "Сумма",
        Step.TERMS: "Условия",
        Step.HOURS: "Срок",
        Step.FEE: "Комиссия",
        Step.ADDRESS: "Адрес выплаты",
        Step.COUNTERPARTY: "Контрагент",
    }
    buttons = [_btn(labels[s], WizCb(a=WizAct.GOTO, val=s.value)) for s in steps if s is not Step.PREVIEW]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([_btn("⬅️ К предпросмотру", WizCb(a=WizAct.GOTO, val=Step.PREVIEW.value))])
    return _markup(rows)


# ── deal card ────────────────────────────────────────────────────────────────


def deal_card_kb(deal: Deal, viewer_id: int, *, bot_username: str) -> InlineKeyboardMarkup:
    v = deal.version

    def cb(action: DealAct) -> DealCb:
        return DealCb(a=action, id=deal.id, v=v)

    rows: list[list[InlineKeyboardButton]] = []
    is_creator = viewer_id == deal.creator_id
    if deal.status is DealStatus.CREATED:
        if is_creator:
            if deal.pending_counterparty_id:
                rows.append(
                    [
                        _btn("✅ Это мой контрагент", cb(DealAct.CONFIRM_CANDIDATE)),
                        _btn("🚫 Отклонить", cb(DealAct.REJECT_CANDIDATE)),
                    ]
                )
            rows.append([_btn("✏️ Изменить", cb(DealAct.EDIT)), _btn("✖️ Отменить", cb(DealAct.CANCEL))])
            if deal.invite_token and deal.counterparty_of(viewer_id) is None:
                link = invite_url(bot_username, deal)
                text = f"Сделка {deal.code} через гаранта @{bot_username}: откройте ссылку и примите условия"
                rows.append(
                    [InlineKeyboardButton(text="📤 Отправить приглашение", url=share_url(link, text))]
                )
        else:
            rows.append(
                [_btn("✅ Принять условия", cb(DealAct.ACCEPT)), _btn("✖️ Отказаться", cb(DealAct.CANCEL))]
            )
    elif deal.status is DealStatus.ACCEPTED:
        row = [_btn("✏️ Изменить", cb(DealAct.EDIT))] if is_creator else []
        rows.append([*row, _btn("✖️ Отменить", cb(DealAct.CANCEL))])
    if deal.status in TERMINAL_STATUSES and deal.party_role(viewer_id) is not None:
        rows.append(
            [_btn("🔁 Повторить", cb(DealAct.REPEAT)), _btn("📑 В шаблоны", cb(DealAct.SAVE_TEMPLATE))]
        )
    rows.append([_btn("🔄 Обновить", cb(DealAct.VIEW))])
    return _markup(rows)


def cancel_confirm_kb(deal: Deal) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                _btn("Да, отменить", DealCb(a=DealAct.CANCEL_CONFIRM, id=deal.id, v=deal.version)),
                _btn("Нет", DealCb(a=DealAct.VIEW, id=deal.id, v=deal.version)),
            ],
        ]
    )


def deal_list_kb(deals: Sequence[tuple[Deal, str]], *, page: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = [[_btn(label, DealCb(a=DealAct.VIEW, id=deal.id, v=deal.version))] for deal, label in deals]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️", MenuCb(a=MenuAct.DEALS, page=page - 1)))
    if has_more:
        nav.append(_btn("➡️", MenuCb(a=MenuAct.DEALS, page=page + 1)))
    rows += [nav, [_btn("⬅️ В меню", MenuCb(a=MenuAct.MAIN))]]
    return _markup(rows)


def templates_kb(templates: Sequence[DealTemplate]) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"📑 {t.name}", TplCb(a=TplAct.USE, id=t.id)), _btn("🗑", TplCb(a=TplAct.DELETE, id=t.id))]
        for t in templates
    ]
    rows.append([_btn("⬅️ В меню", MenuCb(a=MenuAct.MAIN))])
    return _markup(rows)


def inline_open_kb(bot_username: str, token: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(
                    text="🛡 Открыть сделку в боте", url=f"https://t.me/{bot_username}?start=i_{token}"
                )
            ]
        ]
    )


def _short(address: str) -> str:
    return address if len(address) <= 16 else f"{address[:8]}…{address[-6:]}"


def network_title(specs: Sequence[AssetSpec], network: Network) -> str:
    for spec in specs:
        if spec.network is network:
            return spec.title
    return T.NETWORK_SHORT[network]
