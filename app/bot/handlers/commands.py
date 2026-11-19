"""One-line deal commands: /sell, /buy, /deal ``[@username] <amount> [USDT] [network] [terms]``."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import texts as T
from app.bot.context import BotContext
from app.bot.drafts import DealDraft
from app.bot.handlers.wizard import start_wizard
from app.bot.parsing import parse_deal_text
from app.db.models import User
from app.domain.enums import DealSource, PartyRole
from app.domain.errors import DomainError
from app.services.queries import last_defaults
from app.services.users import find_by_username

ROLE_BY_COMMAND = {"sell": PartyRole.SELLER, "buy": PartyRole.BUYER, "deal": None}


async def quick_deal(
    message: Message, command: CommandObject, state: FSMContext, ctx: BotContext, user: User, bot: Bot
) -> None:
    role = ROLE_BY_COMMAND[command.command.lower()]
    parsed = parse_deal_text(command.args or "")  # ParseError → friendly message via the error handler
    async with ctx.uow() as uow:
        defaults = await last_defaults(uow.session, user.id)
        known = await find_by_username(uow.session, parsed.username) if parsed.username else None

    draft = DealDraft(
        role=role,
        network=ctx.default_network(parsed.network or defaults.network),
        terms=parsed.terms,
        hours=defaults.hours,
        fee_payer=defaults.fee_payer,
        source=DealSource.COMMAND,
    )
    if parsed.username:
        if user.username and parsed.username.lower() == user.username.lower():
            await message.answer("Нельзя создать сделку с самим собой.")
            return
        if known is not None:
            draft.set_counterparty(user_id=known.id, username=None, label=f"@{known.username}")
        else:
            draft.set_counterparty(user_id=None, username=parsed.username, label=f"@{parsed.username}")

    notice = None
    if parsed.amount is not None and draft.network is not None:
        try:
            async with ctx.uow() as uow:
                await uow.deals.check_amount(user.id, draft.network, parsed.amount)
            draft.amount = f"{parsed.amount.normalize():f}"
        except DomainError as exc:
            notice = T.error_text(exc)
    await start_wizard(bot, message.chat.id, state, ctx, user, draft, notice=notice)


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="commands")
    router.message.register(quick_deal, Command("sell", "buy", "deal"))
    return router
