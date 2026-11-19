"""Flags in deal-chat messages that usually mean "pay me outside the bot" or a phishing attempt.

The message is still delivered (blocking would push people to other channels); the recipient sees
a warning and the flags are stored for the arbitrator.
"""

from __future__ import annotations

import re

_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_CRYPTO_RES = (
    re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"),  # TRON
    re.compile(r"\b0x[0-9a-fA-F]{40}\b"),  # EVM
    re.compile(r"\b(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}\b"),  # TON user-friendly
    re.compile(r"\b(?:bc1)[a-z0-9]{25,59}\b"),  # BTC bech32
    re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),  # Solana / base58 keys
)
_LINK_RE = re.compile(r"(?:https?://|www\.|\bt\.me/|\btelegram\.me/)", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan(text: str | None) -> list[str]:
    if not text:
        return []
    flags: list[str] = []
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            flags.append("card")
            break
    if any(r.search(text) for r in _CRYPTO_RES):
        flags.append("crypto_address")
    if _LINK_RE.search(text):
        flags.append("link")
    if _MENTION_RE.search(text):
        flags.append("mention")
    return flags
