"""Deal chat: the parties talk through the bot; every message is stored for the arbitrator.

Messages are relayed immediately (not via the outbox): it is a conversation, and the sender is told
right away when the other side cannot receive messages.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as K
from app.bot.callbacks import DealAct, DealCb
from app.bot.chat_filters import scan
from app.bot.context import BotContext
from app.bot.keyboards import CHAT_STATUSES
from app.bot.texts import esc
from app.db.models import Deal, DealMessage, User
from app.domain.enums import PartyRole
from app.domain.errors import Forbidden, InvalidTransition

SENDER = {PartyRole.BUYER: "покупателя", PartyRole.SELLER: "продавца"}
RECIPIENT = {PartyRole.BUYER: "продавцу", PartyRole.SELLER: "покупателю"}
WARNING = (
    "\n\n⚠️ <b>В сообщении есть реквизиты, адрес или ссылка.</b> Не платите и не переходите мимо "
    "бота: гарант защищает только оплату через бота. Бот никогда не просит перевести деньги напрямую."
)


class ChatSG(StatesGroup):
    active = State()


def _file(message: Message) -> tuple[str | None, str | None]:
    if message.photo:
        return message.photo[-1].file_id, "photo"
    for kind in ("document", "video", "voice", "audio", "animation", "video_note", "sticker"):
        item = getattr(message, kind)
        if item is not None:
            return item.file_id, kind
    return None, None


async def _chat_deal(ctx: BotContext, user: User, deal_id: int) -> tuple[Deal, PartyRole, int]:
    async with ctx.uow() as uow:
        deal = await uow.deals.get_for_user(user.id, deal_id)
    role = deal.party_role(user.id)
    other = deal.counterparty_of(user.id)
    if role is None or other is None:
        raise Forbidden("only the two parties can use the deal chat")
    if deal.status not in CHAT_STATUSES:
        raise InvalidTransition("the deal chat is closed")
    return deal, role, other


async def on_chat_open(
    callback: CallbackQuery, callback_data: DealCb, ctx: BotContext, user: User, state: FSMContext, bot: Bot
) -> None:
    deal, role, _ = await _chat_deal(ctx, user, callback_data.id)
    await state.set_state(ChatSG.active)
    await state.update_data(chat_deal_id=deal.id)
    await callback.answer()
    await bot.send_message(
        user.id,
        f"💬 <b>Чат по сделке {esc(deal.code)}</b>\n\nВсё, что вы напишете, бот перешлёт {RECIPIENT[role]}. "
        "Можно отправлять текст, фото и файлы. Переписка сохраняется и будет видна арбитру, если откроется "
        "спор.\n\nВыйти из чата — /done или кнопкой ниже.",
        reply_markup=K.chat_close_kb(deal),
    )


async def on_chat_message(message: Message, ctx: BotContext, user: User, state: FSMContext, bot: Bot) -> None:
    deal_id = (await state.get_data()).get("chat_deal_id")
    if deal_id is None:
        await state.clear()
        return
    try:
        deal, role, other = await _chat_deal(ctx, user, deal_id)
    except (Forbidden, InvalidTransition):
        await state.clear()
        await message.answer("Чат этой сделки закрыт.")
        return
    text = message.text or message.caption
    file_id, file_type = _file(message)
    flags = scan(text)
    async with ctx.uow() as uow:
        uow.session.add(
            DealMessage(
                deal_id=deal.id,
                sender_id=user.id,
                sender_role=role.value,
                text=text,
                file_id=file_id,
                file_type=file_type,
                flagged=flags,
            )
        )
    header = f"💬 <b>Сделка {esc(deal.code)}</b>, сообщение от {SENDER[role]}:"
    warning = WARNING if flags else ""
    try:
        if message.text is not None:
            await bot.send_message(
                other, f"{header}\n\n{esc(message.text)}{warning}", reply_markup=K.chat_relay_kb(deal)
            )
        else:
            caption = f"{header}\n\n{esc(message.caption or '')}{warning}"
            await bot.copy_message(
                chat_id=other,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=caption[:1024],
                reply_markup=K.chat_relay_kb(deal),
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer(
            "⚠️ Не доставлено: собеседник заблокировал бота или ещё не открывал его. "
            "Сообщение сохранено в истории сделки."
        )
        return
    if flags:
        await message.answer("Доставлено. ⚠️ Напоминаем: платите только через бота, не по реквизитам из чата.")


async def on_chat_close(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Чат закрыт. Открыть снова — кнопкой «Написать второй стороне» в карточке сделки.")


async def on_chat_close_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Чат закрыт")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


def build_router() -> Router:
    router = Router(name="chat")
    router.callback_query.register(on_chat_open, DealCb.filter(F.a == DealAct.CHAT))
    router.callback_query.register(on_chat_close_button, DealCb.filter(F.a == DealAct.CHAT_CLOSE))
    router.message.register(on_chat_close, ChatSG.active, Command("done", "cancel"))
    router.message.register(on_chat_message, ChatSG.active)
    return router
