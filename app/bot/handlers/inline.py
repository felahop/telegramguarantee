"""Inline mode: ``@bot [@username] 150 USDT [network] описание`` in any chat → a deal card with a
deep link. The deal is created when somebody opens the link (not on every keystroke).

Security: the card's button is a URL into the bot's private chat, never a callback — whoever opens
it from a group must still be confirmed by the creator unless the creator named them via @username.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from decimal import Decimal

from aiogram import Bot, Router
from aiogram.types import (
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InlineQueryResultUnion,
    InputTextMessageContent,
    Message,
)

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.context import BotContext
from app.bot.parsing import ParseError, parse_deal_text
from app.bot.render import render_inline_card
from app.bot.texts import esc
from app.bot.views import deal_view
from app.db.models import User
from app.domain.enums import DealSource, FeePayer, Network, PartyRole, Provider
from app.domain.errors import DomainError
from app.domain.fees import FeePolicy, calculate_fees
from app.services.deals import NewDeal
from app.services.queries import last_defaults, saved_addresses

DRAFT_TTL = 24 * 3600
CREATING = "creating"


@dataclass
class InlineDraft:
    creator_id: int
    role: str
    network: str
    amount: str
    terms: str
    hours: int
    fee_payer: str
    username: str | None
    payout_address: str | None
    inline_message_id: str | None = None

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str) -> InlineDraft:
        return cls(**json.loads(raw))


async def _store_draft(ctx: BotContext, draft: InlineDraft) -> str:
    """Same query by the same user → same token (no key churn while the user types)."""
    fingerprint = hashlib.sha256(
        json.dumps(
            [draft.creator_id, draft.role, draft.network, draft.amount, draft.terms, draft.username]
        ).encode()
    ).hexdigest()[:32]
    token = await ctx.kv.get(f"inlq:{fingerprint}")
    if token is None:
        token = secrets.token_urlsafe(12)
        await ctx.kv.set(f"inlq:{fingerprint}", token, DRAFT_TTL)
    await ctx.kv.set(f"inl:{token}", draft.dumps(), DRAFT_TTL)
    return token


def _article(article_id: str, title: str, text: str, description: str = "") -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=article_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=text),
    )


async def on_inline_query(query: InlineQuery, ctx: BotContext, user: User) -> None:
    if user.started_at is None:
        # The creator must have started the bot: otherwise we could not ask them to confirm
        # the counterparty or tell them about the payment.
        await query.answer(
            [],
            cache_time=0,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Сначала откройте бота — это займёт секунду", start_parameter="inline"
            ),
        )
        return
    hint = _article(
        "hint",
        "Как создать сделку",
        f"Пример: <code>@{esc(ctx.bot_username)} 150 USDT trc20 описание сделки</code>",
        "Напишите: [@username] сумма USDT [сеть] описание",
    )
    try:
        parsed = parse_deal_text(query.query)
    except ParseError as exc:
        await query.answer(
            [_article("error", "Не понял запрос", T.error_text(exc), T.error_text(exc)[:100]), hint],
            cache_time=0,
            is_personal=True,
        )
        return
    if parsed.amount is None or not parsed.terms:
        await query.answer([hint], cache_time=0, is_personal=True)
        return

    async with ctx.uow() as uow:
        defaults = await last_defaults(uow.session, user.id)
        network = ctx.default_network(parsed.network or defaults.network)
        try:
            await uow.deals.check_amount(user.id, network, parsed.amount)
        except DomainError as exc:
            await query.answer(
                [_article("error", "Нельзя создать сделку", T.error_text(exc), T.error_text(exc)[:100])],
                cache_time=0,
                is_personal=True,
            )
            return
        saved = await saved_addresses(uow.session, user.id, network)

    spec = ctx.spec(network)
    policy = FeePolicy.from_settings(ctx.settings, spec)
    results: list[InlineQueryResultUnion] = []
    for role in (PartyRole.SELLER, PartyRole.BUYER):
        payout_address = None
        if role is PartyRole.SELLER and network.provider is Provider.ONCHAIN:
            if not saved:
                results.append(
                    _article(
                        "need-address",
                        "Чтобы продавать — сохраните адрес выплаты",
                        "Создайте любую сделку продавцом в боте — адрес выплаты сохранится "
                        "для inline-режима.",
                        f"Для сети {spec.title} нужен адрес выплаты",
                    )
                )
                continue
            payout_address = saved[0].address
        try:
            breakdown = calculate_fees(spec.minor(parsed.amount), defaults.fee_payer, policy)
        except DomainError as exc:
            results.append(_article(f"error-{role}", "Сумма слишком мала", T.error_text(exc)))
            continue
        draft = InlineDraft(
            creator_id=user.id,
            role=role.value,
            network=network.value,
            amount=f"{parsed.amount.normalize():f}",
            terms=parsed.terms,
            hours=defaults.hours,
            fee_payer=defaults.fee_payer.value,
            username=parsed.username,
            payout_address=payout_address,
        )
        token = await _store_draft(ctx, draft)
        card = render_inline_card(
            creator=user,
            role=role,
            counterparty_username=parsed.username,
            breakdown=breakdown,
            spec=spec,
            fee_payer=defaults.fee_payer,
            fee_bps=ctx.settings.fee_rate_bps,
            hours=defaults.hours,
            terms=parsed.terms,
            network_fee_payer=ctx.settings.network_fee_payer,
            bot_username=ctx.bot_username,
        )
        title = "💼 Я продаю" if role is PartyRole.SELLER else "🛒 Я покупаю"
        results.append(
            InlineQueryResultArticle(
                id=f"{role.value}:{token}",
                title=f"{title} · {parsed.amount.normalize():f} USDT · {spec.title}",
                description=parsed.terms[:100],
                input_message_content=InputTextMessageContent(message_text=card),
                reply_markup=K.inline_open_kb(ctx.bot_username, token),
            )
        )
    await query.answer(results, cache_time=0, is_personal=True)


async def on_chosen(result: ChosenInlineResult, ctx: BotContext) -> None:
    """Needs /setinlinefeedback: remembers the posted card so it can be updated later."""
    if ":" not in result.result_id or not result.inline_message_id:
        return
    token = result.result_id.split(":", 1)[1]
    raw = await ctx.kv.get(f"inl:{token}")
    if raw is None:
        return
    draft = InlineDraft.loads(raw)
    draft.inline_message_id = result.inline_message_id
    await ctx.kv.set(f"inl:{token}", draft.dumps(), DRAFT_TTL)


async def open_inline_draft(message: Message, ctx: BotContext, user: User, token: str, bot: Bot) -> None:
    """Deep link ``i_<token>``: materialize the deal once, then show it to whoever opened the link."""
    raw = await ctx.kv.get(f"inl:{token}")
    if raw is None:
        await message.answer("Ссылка устарела. Попросите собеседника отправить сделку заново.")
        return
    draft = InlineDraft.loads(raw)
    lock_key = f"inld:{token}"
    deal_id_raw = await ctx.kv.get(lock_key)
    if deal_id_raw is None and user.id == draft.creator_id:
        await message.answer(
            "Это ваша сделка: её примет тот, кто откроет карточку по кнопке. "
            "После этого вы получите запрос на подтверждение."
        )
        return
    if deal_id_raw is None:
        if not await ctx.kv.set_nx(lock_key, CREATING, DRAFT_TTL):
            deal_id_raw = await ctx.kv.get(lock_key)
        else:
            try:
                async with ctx.uow() as uow:
                    deal = await uow.deals.create_deal(
                        NewDeal(
                            creator_id=draft.creator_id,
                            creator_role=PartyRole(draft.role),
                            network=Network(draft.network),
                            amount=Decimal(draft.amount),
                            terms=draft.terms,
                            execution_hours=draft.hours,
                            fee_payer=FeePayer(draft.fee_payer),
                            counterparty_username=draft.username,
                            seller_payout_address=draft.payout_address,
                            source=DealSource.INLINE,
                        )
                    )
                    deal.inline_message_id = draft.inline_message_id
                    deal_id_raw = str(deal.id)
            except Exception:
                await ctx.kv.delete(lock_key)
                raise
            await ctx.kv.set(lock_key, deal_id_raw, DRAFT_TTL)
    if deal_id_raw in (None, CREATING):
        await message.answer("Сделка создаётся, откройте ссылку ещё раз через пару секунд.")
        return
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(draft.creator_id, int(deal_id_raw))
        if user.id != deal.creator_id:
            deal = await uow.deals.open_invite(user.id, deal.invite_token or "")
        text, markup = await deal_view(ctx, uow.session, deal, user.id)
    await message.answer(text, reply_markup=markup)


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="inline")
    router.inline_query.register(on_inline_query)
    router.chosen_inline_result.register(on_chosen)
    return router
