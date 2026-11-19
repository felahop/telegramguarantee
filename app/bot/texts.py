"""User-facing Russian texts. Everything a user reads lives here (single place to proofread/translate).

All dynamic user content (terms, names) must go through ``esc`` — messages use HTML parse mode.
"""

from __future__ import annotations

import html

from app.domain.enums import DealStatus, FeePayer, Network, NetworkFeePayer, PartyRole
from app.domain.errors import DomainError

esc = html.escape

STATUS = {
    DealStatus.CREATED: "⏳ Ожидает принятия",
    DealStatus.ACCEPTED: "🤝 Условия приняты, ожидает оплаты",
    DealStatus.AWAITING_PAYMENT: "💳 Ожидает оплаты",
    DealStatus.FUNDED: "🔒 Оплачено, деньги у гаранта",
    DealStatus.SELLER_CONFIRMED: "📦 Продавец выполнил обязательства",
    DealStatus.BUYER_CONFIRMED: "💸 Выплата продавцу",
    DealStatus.PAYOUT_PENDING: "💸 Выплата продавцу",
    DealStatus.COMPLETED: "✅ Завершена",
    DealStatus.CANCELLED: "✖️ Отменена",
    DealStatus.EXPIRED: "⌛ Истекла",
    DealStatus.DISPUTED: "⚖️ Спор",
    DealStatus.RESOLVED_BUYER: "⚖️ Спор решён",
    DealStatus.RESOLVED_SELLER: "⚖️ Спор решён",
    DealStatus.SPLIT: "⚖️ Спор решён",
    DealStatus.REFUND_PENDING: "↩️ Возврат покупателю",
    DealStatus.REFUNDED: "↩️ Деньги возвращены покупателю",
}

ROLE = {PartyRole.BUYER: "Покупатель", PartyRole.SELLER: "Продавец"}
ROLE_YOU = {PartyRole.BUYER: "Вы — покупатель", PartyRole.SELLER: "Вы — продавец"}
FEE_PAYER = {
    FeePayer.BUYER: "платит покупатель",
    FeePayer.SELLER: "платит продавец",
    FeePayer.SPLIT: "пополам",
}
FEE_PAYER_BUTTON = {FeePayer.BUYER: "Покупатель", FeePayer.SELLER: "Продавец", FeePayer.SPLIT: "Пополам"}
NETWORK_FEE_PAYER = {NetworkFeePayer.SELLER: "с продавца", NetworkFeePayer.SERVICE: "за счёт сервиса"}
NETWORK_SHORT = {
    Network.CRYPTOBOT: "CryptoBot",
    Network.TRON: "TRC-20",
    Network.BSC: "BEP-20",
    Network.ETH: "ERC-20",
    Network.TON: "TON",
    Network.SOL: "Solana",
}

SAFETY_FOOTER = (
    "🔐 Проверяйте сделку только в этом боте. Бот никогда не просит перевести деньги "
    "«напрямую» продавцу или на другой адрес. Скриншот оплаты — не доказательство: статус "
    "оплаты определяет только бот."
)

WELCOME = (
    "🛡 <b>Гарант P2P-сделок</b>\n\n"
    "Бот держит оплату покупателя и отдаёт её продавцу только после того, как обе стороны "
    "подтвердили выполнение сделки. Расчёты — в USDT.\n\n"
    "Как это работает:\n"
    "1. Одна сторона создаёт сделку, вторая принимает условия.\n"
    "2. Покупатель оплачивает — бот сам проверяет поступление.\n"
    "3. Продавец выполняет обязательства и нажимает «Я выполнил».\n"
    "4. Покупатель подтверждает получение — бот выплачивает продавцу.\n\n"
    "Быстрый способ: <code>/sell @username 150 USDT trc20 описание</code> "
    "или <code>/buy …</code>.\n\n" + SAFETY_FOOTER
)

HELP = (
    "<b>Создать сделку</b>\n"
    "• Кнопка «Новая сделка» — пошаговый мастер.\n"
    "• <code>/sell @username 150 USDT trc20 описание</code> — вы продаёте.\n"
    "• <code>/buy @username 150 USDT описание</code> — вы покупаете.\n"
    "• В любом чате: <code>@{bot} 150 USDT описание</code> — карточка сделки для собеседника.\n\n"
    "<b>Сети</b>: cryptobot, trc20, bep20, erc20, ton, sol. Без сети — последняя использованная.\n\n"
    "<b>Важно</b>\n"
    "• Деньги не уходят продавцу без подтверждения покупателя — только через арбитра.\n"
    "• После оплаты отменить сделку можно только по согласию обеих сторон, иначе — спор.\n\n" + SAFETY_FOOTER
)

ASK = {
    "role": "Шаг 1. Кто вы в этой сделке?",
    "network": "Шаг 2. В какой сети будет оплата в USDT?",
    "amount": "Шаг 3. Сумма сделки в USDT. Выберите или напишите число, например <code>150</code> "
    "или <code>99.90</code>.",
    "terms": "Шаг 4. Опишите условия: что именно и в каком виде передаётся. Арбитр в случае спора "
    "будет опираться на этот текст.",
    "hours": "Шаг 5. Сколько времени у продавца на выполнение после оплаты?",
    "hours_custom": "Напишите срок: например <code>12</code> (часы), <code>2д</code> "
    "или <code>5 дней</code>.",
    "fee": "Шаг 6. Кто платит комиссию сервиса?",
    "address": "Адрес для выплаты в сети <b>{network}</b>. Пришлите адрес кошелька или выберите сохранённый. "
    "Изменить его можно будет только до оплаты.",
    "counterparty": "Последний шаг. С кем сделка?\n\nНапишите @username, выберите контакт или получите "
    "ссылку-приглашение, чтобы отправить её самостоятельно.",
    "contact": "Нажмите кнопку ниже и выберите контакт.",
}

DEADLINE_LABELS = {1: "1 час", 24: "24 часа", 72: "3 дня"}


def hours_label(hours: int) -> str:
    if hours in DEADLINE_LABELS:
        return DEADLINE_LABELS[hours]
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} {_plural(days, 'день', 'дня', 'дней')}"
    return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def deals_count(n: int) -> str:
    return f"{n} {_plural(n, 'сделка', 'сделки', 'сделок')}"


# ── errors ───────────────────────────────────────────────────────────────────

_PARSE_REASONS = {
    "bad_username": "Неверный @username: 4–32 символа, латиница, цифры и «_».",
    "no_amount": "Не вижу суммы. Пример: <code>/sell @username 150 USDT trc20 описание</code>",
    "unsupported_currency": "Сделки проводятся только в USDT.",
    "bad_hours": "Не понял срок. Примеры: <code>12</code>, <code>2д</code>, <code>5 дней</code>.",
    "hours_range": "Срок должен быть от 1 часа до {max} часов.",
}

_VALIDATION_FIELDS = {
    "terms": "Условия должны быть от {min} до {max} символов.",
    "execution_hours": "Срок выполнения должен быть от 1 часа до {max} часов.",
    "seller_payout_address": "Нужен адрес для выплаты продавцу.",
    "address": "Это адрес самого бота — укажите свой кошелёк.",
    "counterparty": "Нельзя создать сделку с самим собой.",
    "comment": "Нужен комментарий к решению.",
}

_GENERIC = {
    "not_found": "Сделка не найдена.",
    "forbidden": "Это действие вам недоступно.",
    "invalid_transition": "Это действие уже выполнено или недоступно в текущем статусе сделки.",
    "version_conflict": "Условия сделки изменились. Проверьте актуальную карточку.",
    "invalid_address": "Неверный адрес для этой сети. Проверьте и пришлите ещё раз.",
    "invalid_amount": "Неверная сумма.",
    "idempotency_conflict": "Конфликт повторной операции. Обновите карточку.",
    "provider_unavailable": "Оплата в этой сети сейчас недоступна. Попробуйте чуть позже.",
    "confirmation_expired": "Подтверждение устарело. Нажмите кнопку ещё раз в актуальной карточке сделки.",
}


def error_text(exc: DomainError) -> str:  # noqa: PLR0911 - one message per error kind
    """Human message for a domain error. Never exposes internals."""
    ctx = exc.context
    if exc.code == "parse_error":
        template = _PARSE_REASONS.get(str(ctx.get("reason")), "Не удалось разобрать команду.")
        return template.format(**{k: esc(str(v)) for k, v in ctx.items()})
    if exc.code == "limit_exceeded":
        kind = ctx.get("kind")
        limit = esc(str(ctx.get("limit", "")))
        if kind == "min":
            return f"Минимальная сумма сделки — {limit} USDT."
        if kind == "max":
            return f"Максимальная сумма для вашего аккаунта — {limit} USDT."
        if kind == "counterparty_max":
            return f"Для аккаунта второй стороны лимит — {limit} USDT. Уменьшите сумму."
        return "Превышен лимит."
    if exc.code == "invalid_amount" and "too small" in exc.message:
        return "Сумма слишком мала: после комиссий продавцу ничего не останется."
    if exc.code == "validation_failed" and ctx.get("field") in _VALIDATION_FIELDS:
        return _VALIDATION_FIELDS[str(ctx["field"])].format(**{k: esc(str(v)) for k, v in ctx.items()})
    if exc.code == "validation_failed" and "network is not available" in exc.message:
        return "Эта сеть сейчас недоступна."
    if exc.code == "forbidden" and "banned" in exc.message:
        return "Аккаунт заблокирован."
    if exc.code == "forbidden" and "another" in exc.message:
        return "Эта сделка предназначена другому пользователю или уже занята."
    return _GENERIC.get(exc.code, "Не удалось выполнить действие.")


UNEXPECTED_ERROR = "Что-то пошло не так. Попробуйте ещё раз чуть позже."
RATE_LIMITED = "Слишком много действий. Подождите немного."
BANNED = "Ваш аккаунт заблокирован. Если это ошибка — обратитесь в поддержку."
