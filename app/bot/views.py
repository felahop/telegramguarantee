"""Shared view helpers: deal card for a viewer, the single editable wizard message, drafts from deals."""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.context import BotContext
from app.bot.drafts import DealDraft
from app.bot.keyboards import deal_card_kb
from app.bot.render import PartyInfo, render_deal_card
from app.db.models import Deal, DealTemplate, PaymentRequest
from app.domain.enums import DealSource, DealStatus, PartyRole, PaymentRequestStatus
from app.domain.money import from_minor
from app.services.queries import load_users

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove


async def deal_view(
    ctx: BotContext, session: AsyncSession, deal: Deal, viewer_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    users = await load_users(
        session, [deal.buyer_id, deal.seller_id, deal.invited_user_id, deal.pending_counterparty_id]
    )
    parties = {uid: PartyInfo.from_user(u) for uid, u in users.items()}
    payment = None
    if deal.status is DealStatus.AWAITING_PAYMENT:
        payment = (
            await session.execute(
                sa.select(PaymentRequest).where(
                    PaymentRequest.deal_id == deal.id, PaymentRequest.status == PaymentRequestStatus.ACTIVE
                )
            )
        ).scalar_one_or_none()
    text = render_deal_card(
        deal,
        viewer_id=viewer_id,
        parties=parties,
        spec=ctx.spec(deal.network),
        now=ctx.clock(),
        payment=payment,
    )
    return text, deal_card_kb(deal, viewer_id, bot_username=ctx.bot_username, payment=payment)


class Screen:
    """The wizard's single message: edited in place; re-sent when it cannot be edited."""

    def __init__(self, bot: Bot, chat_id: int, state: FSMContext) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._state = state

    async def show(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        msg_id = (await self._state.get_data()).get("msg_id")
        if msg_id:
            try:
                await self._bot.edit_message_text(
                    text=text, chat_id=self._chat_id, message_id=msg_id, reply_markup=markup
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
        sent = await self._bot.send_message(self._chat_id, text, reply_markup=markup)
        await self._state.update_data(msg_id=sent.message_id)

    async def send_new(self, text: str, markup: Markup | None = None) -> None:
        """A separate message (e.g. one carrying a reply keyboard); the next screen goes below it."""
        await self._bot.send_message(self._chat_id, text, reply_markup=markup)
        await self._state.update_data(msg_id=None)


def _amount_str(amount: int, decimals: int) -> str:
    return f"{from_minor(amount, decimals).normalize():f}"


def draft_for_edit(deal: Deal) -> DealDraft:
    return DealDraft(
        role=deal.creator_role,
        network=deal.network,
        amount=_amount_str(deal.amount, deal.decimals),
        terms=deal.terms,
        hours=deal.execution_hours,
        fee_payer=deal.fee_payer,
        payout_address=deal.seller_payout_address if deal.creator_role is PartyRole.SELLER else None,
        counterparty_chosen=True,
        source=deal.source,
        edit_deal_id=deal.id,
        edit_version=deal.version,
    )


def draft_for_repeat(deal: Deal, viewer_id: int, counterparty_label: str | None) -> DealDraft:
    role = deal.party_role(viewer_id) or deal.creator_role
    other = deal.counterparty_of(viewer_id)
    draft = DealDraft(
        role=role,
        network=deal.network,
        amount=_amount_str(deal.amount, deal.decimals),
        terms=deal.terms,
        hours=deal.execution_hours,
        fee_payer=deal.fee_payer,
        payout_address=deal.seller_payout_address if role is PartyRole.SELLER else None,
        source=DealSource.REPEAT,
    )
    if other is not None:
        draft.set_counterparty(user_id=other, username=None, label=counterparty_label)
    return draft


def draft_from_template(template: DealTemplate, decimals: int) -> DealDraft:
    return DealDraft(
        role=template.role,
        network=template.network,
        amount=_amount_str(template.amount, decimals) if template.amount is not None else None,
        terms=template.terms,
        hours=template.execution_hours,
        fee_payer=template.fee_payer,
        source=DealSource.TEMPLATE,
    )


def amount_text(value: Decimal) -> str:
    return f"{value.normalize():f}"
