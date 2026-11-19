"""/start (with deep links), /help, main menu, «Мои сделки», «Шаблоны»."""

from __future__ import annotations

import sqlalchemy as sa
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards as K
from app.bot import texts as T
from app.bot.callbacks import MenuAct, MenuCb, TplAct, TplCb
from app.bot.context import BotContext
from app.bot.drafts import DealDraft
from app.bot.handlers.inline import open_inline_draft
from app.bot.handlers.wizard import start_wizard
from app.bot.render import deal_list_line
from app.bot.views import deal_view, draft_from_template
from app.db.models import Deal, User
from app.domain.errors import NotFound
from app.services.queries import delete_template, get_template, list_templates, user_deals

DEALS_PAGE = 8


async def _mark_started(ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        await uow.session.execute(
            sa.update(User)
            .where(User.id == user.id)
            .values(started_at=sa.func.coalesce(User.started_at, ctx.clock()), bot_blocked=False)
        )
    user.started_at = user.started_at or ctx.clock()


async def on_start_deeplink(
    message: Message, command: CommandObject, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    await _mark_started(ctx, user)
    await state.clear()
    arg = command.args or ""
    if arg.startswith("j_"):
        async with ctx.uow() as uow:
            deal = await uow.deals.open_invite(user.id, arg[2:])
            text, markup = await deal_view(ctx, uow.session, deal, user.id)
        await message.answer(text, reply_markup=markup)
    elif arg.startswith("i_"):
        await open_inline_draft(message, ctx, user, arg[2:], bot)
    elif arg.startswith("d_"):
        async with ctx.uow() as uow:
            deal_id = await uow.session.scalar(sa.select(Deal.id).where(Deal.code == arg[2:].upper()))
            if deal_id is None:
                raise NotFound("deal not found")
            deal = await uow.deals.get_for_invitee(user.id, deal_id)
            text, markup = await deal_view(ctx, uow.session, deal, user.id)
        await message.answer(text, reply_markup=markup)
    else:
        await message.answer(T.WELCOME, reply_markup=K.main_menu())


async def on_start(message: Message, ctx: BotContext, user: User, state: FSMContext) -> None:
    await _mark_started(ctx, user)
    await state.clear()
    await message.answer(T.WELCOME, reply_markup=K.main_menu())


async def on_help(message: Message, ctx: BotContext) -> None:
    await message.answer(T.HELP.format(bot=ctx.bot_username), reply_markup=K.back_to_menu())


async def on_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню", reply_markup=K.main_menu())


async def on_new_command(message: Message, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    await start_wizard(bot, message.chat.id, state, ctx, user, DealDraft())


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]


async def on_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(callback, "Главное меню", K.main_menu())


async def on_help_button(callback: CallbackQuery, ctx: BotContext) -> None:
    await _show(callback, T.HELP.format(bot=ctx.bot_username), K.back_to_menu())


async def on_new(callback: CallbackQuery, state: FSMContext, ctx: BotContext, user: User, bot: Bot) -> None:
    await callback.answer()
    msg_id = callback.message.message_id if isinstance(callback.message, Message) else None
    await start_wizard(bot, user.id, state, ctx, user, DealDraft(), msg_id=msg_id)


async def on_deals(callback: CallbackQuery, callback_data: MenuCb, ctx: BotContext, user: User) -> None:
    page = max(callback_data.page, 0)
    async with ctx.uow() as uow:
        deals, has_more = await user_deals(uow.session, user.id, offset=page * DEALS_PAGE, limit=DEALS_PAGE)
    if not deals and page == 0:
        await _show(callback, "У вас пока нет сделок.", K.main_menu())
        return
    items = [(d, deal_list_line(d, user.id)) for d in deals]
    await _show(
        callback,
        "📋 <b>Ваши сделки</b> (активные сверху)",
        K.deal_list_kb(items, page=page, has_more=has_more),
    )


async def on_templates(callback: CallbackQuery, ctx: BotContext, user: User) -> None:
    async with ctx.uow() as uow:
        templates = await list_templates(uow.session, user.id)
    if not templates:
        await _show(
            callback, "Шаблонов пока нет. Сохранить сделку как шаблон можно из её карточки.", K.back_to_menu()
        )
        return
    await _show(callback, "📑 <b>Шаблоны</b>", K.templates_kb(templates))


async def on_use_template(
    callback: CallbackQuery, callback_data: TplCb, state: FSMContext, ctx: BotContext, user: User, bot: Bot
) -> None:
    async with ctx.uow() as uow:
        template = await get_template(uow.session, user.id, callback_data.id)
    draft = draft_from_template(template, ctx.spec(template.network).decimals)
    if draft.network not in ctx.chains.enabled_networks():
        draft.network = None
    await callback.answer()
    msg_id = callback.message.message_id if isinstance(callback.message, Message) else None
    await start_wizard(bot, user.id, state, ctx, user, draft, msg_id=msg_id)


async def on_delete_template(
    callback: CallbackQuery, callback_data: TplCb, ctx: BotContext, user: User
) -> None:
    async with ctx.uow() as uow:
        await delete_template(uow.session, user.id, callback_data.id)
        templates = await list_templates(uow.session, user.id)
    markup = K.templates_kb(templates) if templates else K.back_to_menu()
    await _show(callback, "📑 <b>Шаблоны</b>" if templates else "Шаблонов больше нет.", markup)


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram routers can be attached only once)."""
    router = Router(name="start")
    router.message.register(on_start_deeplink, CommandStart(deep_link=True))
    router.message.register(on_start, CommandStart())
    router.message.register(on_help, Command("help"))
    router.message.register(on_menu, Command("menu", "cancel"))
    router.message.register(on_new_command, Command("new"))
    router.callback_query.register(on_main, MenuCb.filter(F.a == MenuAct.MAIN))
    router.callback_query.register(on_help_button, MenuCb.filter(F.a == MenuAct.HELP))
    router.callback_query.register(on_new, MenuCb.filter(F.a == MenuAct.NEW))
    router.callback_query.register(on_deals, MenuCb.filter(F.a == MenuAct.DEALS))
    router.callback_query.register(on_templates, MenuCb.filter(F.a == MenuAct.TEMPLATES))
    router.callback_query.register(on_use_template, TplCb.filter(F.a == TplAct.USE))
    router.callback_query.register(on_delete_template, TplCb.filter(F.a == TplAct.DELETE))
    return router
