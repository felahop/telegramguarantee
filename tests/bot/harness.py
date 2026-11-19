"""End-to-end bot harness: real Dispatcher + handlers + PostgreSQL, Telegram API recorded in memory."""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChosenInlineResult,
    InlineKeyboardMarkup,
    InlineQuery,
    Message,
    SharedUser,
    Update,
    User,
    UsersShared,
)
from pydantic import SecretStr

from app.bot.context import BotContext
from app.bot.factory import build_bot, build_dispatcher

BOT_ID = 4242


class RecordingSession(BaseSession):
    """Answers every Bot API call locally and records it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.sent_ids: dict[int, int] = {}  # index in calls → message_id given to a sent message
        self.errors: dict[int, Exception] = {}  # chat_id → error raised on sendMessage to that chat
        self._ids = itertools.count(5000)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> TelegramType:
        self.calls.append(method)
        if isinstance(method, SendMessage) and int(method.chat_id) in self.errors:
            raise self.errors[int(method.chat_id)]
        if isinstance(method, SendMessage):
            message_id = next(self._ids)
            self.sent_ids[len(self.calls) - 1] = message_id
            return Message(  # type: ignore[return-value]
                message_id=message_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
                reply_markup=method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None,
            )
        if isinstance(method, EditMessageText | EditMessageReplyMarkup) and method.chat_id is not None:
            return Message(  # type: ignore[return-value]
                message_id=int(method.message_id or 0),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup,
            )
        return True  # type: ignore[return-value]

    async def close(self) -> None:
        return None

    def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError


@dataclass
class Shown:
    """A message (sent or edited) as the user sees it."""

    chat_id: int
    message_id: int | None
    text: str
    markup: Any

    def buttons(self) -> list[tuple[str, str]]:
        """(text, callback_data or url) of every inline button."""
        if not isinstance(self.markup, InlineKeyboardMarkup):
            return []
        return [(b.text, b.callback_data or b.url or "") for row in self.markup.inline_keyboard for b in row]

    def data(self, text_fragment: str) -> str:
        for text, data in self.buttons():
            if text_fragment in text:
                return data
        raise AssertionError(f"no button containing {text_fragment!r} in {self.buttons()}")


@dataclass
class TgUser:
    id: int
    username: str | None
    first_name: str

    def as_tg(self) -> User:
        return User(
            id=self.id, is_bot=False, first_name=self.first_name, username=self.username, language_code="ru"
        )


@dataclass
class BotHarness:
    dp: Dispatcher
    bot: Bot
    session: RecordingSession
    _update_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _message_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    @classmethod
    def create(cls, ctx: BotContext, *, rate_limit: int = 1000) -> BotHarness:
        session = RecordingSession()
        bot = build_bot(ctx.settings.model_copy(update={"bot_token": SecretStr("42:TEST")}), session=session)
        dp = build_dispatcher(ctx, MemoryStorage(), rate_limit=rate_limit)
        return cls(dp=dp, bot=bot, session=session)

    async def _feed(self, update: Update) -> list[TelegramMethod[Any]]:
        before = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        return self.session.calls[before:]

    async def send(self, user: TgUser, text: str) -> list[TelegramMethod[Any]]:
        message = Message(
            message_id=next(self._message_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=user.as_tg(),
            text=text,
        )
        return await self._feed(Update(update_id=next(self._update_ids), message=message))

    async def share_user(self, user: TgUser, shared: TgUser) -> list[TelegramMethod[Any]]:
        message = Message(
            message_id=next(self._message_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=user.as_tg(),
            users_shared=UsersShared(
                request_id=1,
                users=[SharedUser(user_id=shared.id, username=shared.username, first_name=shared.first_name)],
            ),
        )
        return await self._feed(Update(update_id=next(self._update_ids), message=message))

    async def press(
        self, user: TgUser, data: str, *, message_id: int | None = None
    ) -> list[TelegramMethod[Any]]:
        shown = self.last_for(user.id)
        msg_id = message_id if message_id is not None else (shown.message_id if shown else 1)
        message = Message(
            message_id=msg_id or 1,
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="bot"),
            text="card",
        )
        callback = CallbackQuery(
            id=str(next(self._update_ids)),
            from_user=user.as_tg(),
            chat_instance="ci",
            message=message,
            data=data,
        )
        return await self._feed(Update(update_id=next(self._update_ids), callback_query=callback))

    async def inline(self, user: TgUser, query: str) -> list[TelegramMethod[Any]]:
        inline_query = InlineQuery(
            id=str(next(self._update_ids)), from_user=user.as_tg(), query=query, offset=""
        )
        return await self._feed(Update(update_id=next(self._update_ids), inline_query=inline_query))

    async def chosen(self, user: TgUser, result_id: str, inline_message_id: str) -> None:
        chosen = ChosenInlineResult(
            result_id=result_id, from_user=user.as_tg(), query="", inline_message_id=inline_message_id
        )
        await self._feed(Update(update_id=next(self._update_ids), chosen_inline_result=chosen))

    # ── inspection ──────────────────────────────────────────────────────
    def shown(self, chat_id: int) -> list[Shown]:
        """Every message the user saw, in order: sent ones with their new id, edits with the edited id."""
        result = []
        for index, call in enumerate(self.session.calls):
            if isinstance(call, SendMessage) and int(call.chat_id) == chat_id:
                result.append(Shown(chat_id, self.session.sent_ids.get(index), call.text, call.reply_markup))
            elif (
                isinstance(call, EditMessageText)
                and call.chat_id is not None
                and int(call.chat_id) == chat_id
            ):
                result.append(Shown(chat_id, call.message_id, call.text, call.reply_markup))
        return result

    def last_for(self, chat_id: int) -> Shown | None:
        shown = self.shown(chat_id)
        return shown[-1] if shown else None

    def last_text(self, chat_id: int) -> str:
        last = self.last_for(chat_id)
        return last.text if last else ""

    def alerts(self) -> list[str]:
        return [c.text or "" for c in self.session.calls if isinstance(c, AnswerCallbackQuery) and c.text]
