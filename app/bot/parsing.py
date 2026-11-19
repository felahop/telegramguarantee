"""Parsing of one-line deal commands and inline queries.

Accepted shapes (case-insensitive, currency and network in any order, both optional):

    /sell @username 150 USDT trc20 Аккаунт Steam с играми
    /buy 150,50 usdt Дизайн логотипа
    @bot @username 150 USDT ton описание            (inline mode, same grammar)

Only USDT is accepted; another currency is an explicit error rather than silently becoming
part of the terms. The terms keep the user's original spacing and line breaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import Network
from app.domain.errors import AmountError, ValidationFailed
from app.domain.money import parse_user_amount

USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TOKEN_RE = re.compile(r"\s*(\S+)")
_GLUED_AMOUNT_RE = re.compile(r"^([0-9][0-9.,]*)(usdt)$", re.IGNORECASE)

NETWORK_ALIASES: dict[str, Network] = {
    **dict.fromkeys(["cb", "cryptobot", "crypto", "криптобот"], Network.CRYPTOBOT),
    **dict.fromkeys(["trc20", "trc-20", "tron", "trx"], Network.TRON),
    **dict.fromkeys(["bep20", "bep-20", "bsc", "bnb"], Network.BSC),
    **dict.fromkeys(["erc20", "erc-20", "eth", "ethereum"], Network.ETH),
    **dict.fromkeys(["ton", "jetton"], Network.TON),
    **dict.fromkeys(["sol", "solana", "spl"], Network.SOL),
}
_USDT_TOKENS = {"usdt", "usd₮", "usdt₮"}
_OTHER_CURRENCIES = {"usd", "$", "eur", "€", "rub", "руб", "руб.", "₽", "btc", "usdc", "dai", "busd", "тон"}


class ParseError(ValidationFailed):
    code = "parse_error"


@dataclass(frozen=True)
class ParsedDeal:
    username: str | None
    amount: Decimal | None
    network: Network | None
    terms: str | None


def normalize_username(value: str) -> str:
    match = USERNAME_RE.fullmatch(value.strip())
    if not match:
        raise ParseError("bad username", reason="bad_username", value=value)
    return match.group(1)


def parse_deal_text(text: str) -> ParsedDeal:
    """Parse ``[@username] <amount> [USDT] [network] [terms…]``. Empty text → all fields None."""
    rest = text.strip()
    username: str | None = None
    amount: Decimal | None = None
    network: Network | None = None

    token, after = _next_token(rest)
    if token is not None and token.startswith("@"):
        username = normalize_username(token)
        rest = after
        token, after = _next_token(rest)
    if token is None:
        return ParsedDeal(username, None, None, None)

    glued = _GLUED_AMOUNT_RE.fullmatch(token)
    try:
        amount = parse_user_amount(glued.group(1) if glued else token)
    except AmountError as exc:
        raise ParseError("amount expected", reason="no_amount", value=token) from exc
    rest = after

    for _ in range(2):  # currency and network, in any order
        token, after = _next_token(rest)
        if token is None:
            break
        lowered = token.lower()
        if lowered in _USDT_TOKENS:
            rest = after
        elif lowered in NETWORK_ALIASES:
            network = NETWORK_ALIASES[lowered]
            rest = after
        elif lowered in _OTHER_CURRENCIES:
            raise ParseError("only USDT is supported", reason="unsupported_currency", value=token)
        else:
            break
    terms = rest.strip() or None
    return ParsedDeal(username, amount, network, terms)


_HOURS_RE = re.compile(r"^(\d{1,4})\s*([a-zа-яё.]*)$", re.IGNORECASE)
_DAY_UNITS = {"d", "д", "дн", "дн.", "день", "дня", "дней", "сут", "сут.", "сутки", "суток"}
_HOUR_UNITS = {"", "h", "ч", "ч.", "час", "часа", "часов"}


def parse_hours(text: str, *, max_hours: int) -> int:
    """``12``, ``12ч``, ``12 h``, ``3д``, ``3 дня``, ``2 суток`` → hours."""
    match = _HOURS_RE.fullmatch(text.strip())
    if not match:
        raise ParseError("bad duration", reason="bad_hours", value=text)
    number, unit = int(match.group(1)), match.group(2).lower()
    if unit in _DAY_UNITS:
        hours = number * 24
    elif unit in _HOUR_UNITS:
        hours = number
    else:
        raise ParseError("bad duration unit", reason="bad_hours", value=text)
    if not 1 <= hours <= max_hours:
        raise ParseError("duration out of range", reason="hours_range", value=text, max=max_hours)
    return hours


def _next_token(text: str) -> tuple[str | None, str]:
    match = _TOKEN_RE.match(text)
    if not match:
        return None, ""
    return match.group(1), text[match.end() :]
