"""Money primitives.

Rules (enforced here and by tests):
* every amount inside the system is an ``int`` in the asset's minor units (wei-like);
* ``Decimal`` is used only at the edges: user input, provider APIs (strings), display;
* ``float`` is rejected everywhere — it cannot represent 0.1 and silently loses precision.

USDT has different decimals per network (BEP-20: 18, everything else: 6), therefore conversions
always take ``decimals`` explicitly and never assume a global scale.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Context, Decimal, InvalidOperation

from app.domain.enums import Asset, Network
from app.domain.errors import AmountError

# Users see and type USDT with cents.
DISPLAY_PLACES = 2
# The default decimal context keeps only 28 significant digits: an 18-decimal BEP-20 amount above
# 10 billion USDT, or any uint256 value, would be silently rounded. All money operations use this
# context instead; Inexact/Rounded are trapped so precision loss can never be silent.
MONEY_CONTEXT = Context(prec=100, rounding=ROUND_DOWN, traps=[InvalidOperation])
_USER_AMOUNT_RE = re.compile(r"^[0-9]{1,12}(?:\.[0-9]+)?$")  # ASCII digits only


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        # bool is an int subclass; float is the main thing we want to keep out.
        raise TypeError(f"money value must be Decimal, int or str, got {type(value).__name__}")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise AmountError("not a number", value=str(value)) from exc
    if not result.is_finite():
        raise AmountError("amount must be finite", value=str(value))
    return result


def to_minor(value: Decimal | int | str, decimals: int) -> int:
    """Convert a human amount to minor units. Never rounds: excess precision is an error."""
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    scaled = _as_decimal(value).scaleb(decimals, context=MONEY_CONTEXT)
    integral = scaled.to_integral_value(context=MONEY_CONTEXT)
    if scaled != integral:
        raise AmountError("more precision than the asset supports", value=str(value), decimals=decimals)
    return int(integral)


def from_minor(amount: int, decimals: int) -> Decimal:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("minor amount must be int")
    return Decimal(amount).scaleb(-decimals, context=MONEY_CONTEXT)


def cent(decimals: int) -> int:
    """Minor units in 0.01 of the asset (the display granularity)."""
    if decimals < DISPLAY_PLACES:
        raise ValueError("asset must have at least 2 decimals")
    return int(10 ** (decimals - DISPLAY_PLACES))


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return -(-numerator // denominator)


def ceil_to_cent(amount: int, decimals: int) -> int:
    step = cent(decimals)
    return ceil_div(amount, step) * step


def rescale(amount: int, from_decimals: int, to_decimals: int) -> int:
    """Exact conversion between two scales (e.g. 6-decimal config value → 18-decimal BEP-20)."""
    if to_decimals >= from_decimals:
        return amount * int(10 ** (to_decimals - from_decimals))
    factor = int(10 ** (from_decimals - to_decimals))
    if amount % factor:
        raise AmountError(
            "lossy rescale", amount=amount, from_decimals=from_decimals, to_decimals=to_decimals
        )
    return amount // factor


def format_amount(amount: int, decimals: int, *, places: int | None = None) -> str:
    """Render minor units for humans.

    By default shows at least 2 places and as many more as needed to be exact, so a tagged amount
    like 101.9863 is never displayed rounded (the buyer must send it exactly). With explicit
    ``places`` the value is truncated, never rounded up: we must not overstate what someone gets.
    """
    value = from_minor(amount, decimals)
    if places is None:
        exponent = value.normalize(context=MONEY_CONTEXT).as_tuple().exponent
        if not isinstance(exponent, int):  # pragma: no cover - finite by construction
            raise AmountError("non-finite amount")
        places = max(DISPLAY_PLACES, -exponent)
    quantum = Decimal(1).scaleb(-places, context=MONEY_CONTEXT)
    return f"{value.quantize(quantum, rounding=ROUND_DOWN, context=MONEY_CONTEXT):f}"


def parse_user_amount(text: str, *, max_places: int = DISPLAY_PLACES) -> Decimal:
    """Parse an amount typed by a user: ``150``, ``150.5``, ``150,50``, ``1 000``.

    Rejects negatives, exponents, more than ``max_places`` decimals and absurd sizes.
    """
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace("_", "")
    if cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    if not _USER_AMOUNT_RE.fullmatch(cleaned):
        raise AmountError("unparseable amount", text=text)
    value = Decimal(cleaned)
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - regex admits only finite numbers
        raise AmountError("unparseable amount", text=text)
    if -exponent > max_places:
        raise AmountError("too many decimal places", text=text, max_places=max_places)
    if value <= 0:
        raise AmountError("amount must be positive", text=text)
    return value


def asset_code(asset: Asset | str, network: Network | str) -> str:
    """Ledger asset identifier: the same token on two networks is two different assets."""
    return f"{Asset(asset).value}@{Network(network).value}"


_NATIVE_COINS = {
    Network.BSC: "BNB",
    Network.ETH: "ETH",
    Network.TRON: "TRX",
    Network.TON: "TON",
    Network.SOL: "SOL",
}


NATIVE_DECIMALS = {"BNB": 18, "ETH": 18, "TRX": 6, "TON": 9, "SOL": 9}


def native_asset_code(network: Network | str) -> str:
    """Ledger code of the coin that pays gas on a network (network fees are spent in it, not in USDT)."""
    net = Network(network)
    if net not in _NATIVE_COINS:
        raise ValueError(f"{net} has no native gas coin")
    return f"{_NATIVE_COINS[net]}@{net.value}"
