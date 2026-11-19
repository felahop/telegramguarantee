"""Fee and settlement arithmetic — pure functions over int minor units.

``calculate_fees`` produces the numbers shown on the deal card *before* the deal is created;
the ``settle_*`` functions split an escrow when it is released, refunded or decided by an admin.
Every settlement sums exactly to the escrow it splits (property-tested).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AssetSpec, Settings
from app.domain.enums import FeePayer, NetworkFeePayer
from app.domain.errors import AmountError
from app.domain.money import ceil_div, ceil_to_cent, to_minor

BPS = 10_000
# Unique-amount tag: 0.0001 … 0.0999 of the asset (fourth decimal).
TAG_DECIMAL_PLACE = 4
TAG_MAX_STEPS = 999


@dataclass(frozen=True)
class FeePolicy:
    decimals: int
    rate_bps: int
    min_fee: int
    network_fee: int  # fixed payout network fee for this network, minor units
    network_fee_payer: NetworkFeePayer
    min_payout: int

    def __post_init__(self) -> None:
        if not 0 <= self.rate_bps < BPS:
            raise ValueError("rate_bps out of range")
        if min(self.min_fee, self.network_fee, self.min_payout) < 0:
            raise ValueError("fee policy amounts must be non-negative")

    @classmethod
    def from_settings(cls, settings: Settings, spec: AssetSpec) -> FeePolicy:
        return cls(
            decimals=spec.decimals,
            rate_bps=settings.fee_rate_bps,
            min_fee=to_minor(settings.fee_min_usdt, spec.decimals),
            network_fee=spec.network_fee,
            network_fee_payer=settings.network_fee_payer,
            min_payout=spec.min_payout,
        )

    @property
    def user_network_fee(self) -> int:
        """Network fee charged to a user per payout (0 when the service absorbs it)."""
        return self.network_fee if self.network_fee_payer is NetworkFeePayer.SELLER else 0


@dataclass(frozen=True)
class FeeBreakdown:
    amount: int
    fee: int
    buyer_fee: int
    seller_fee: int
    network_fee: int  # charged to the seller on payout (0 if the service pays it)
    buyer_pays: int  # before the unique-amount tag
    seller_receives: int

    @property
    def service_gross(self) -> int:
        return self.fee + self.network_fee


def calculate_fees(amount: int, fee_payer: FeePayer, policy: FeePolicy) -> FeeBreakdown:
    """fee = max(ceil_to_cent(amount × rate), min_fee); split rounds the buyer's half up."""
    if amount <= 0:
        raise AmountError("amount must be positive", amount=amount)
    percent_fee = ceil_to_cent(ceil_div(amount * policy.rate_bps, BPS), policy.decimals)
    fee = max(percent_fee, policy.min_fee)
    if fee_payer is FeePayer.BUYER:
        buyer_fee = fee
    elif fee_payer is FeePayer.SELLER:
        buyer_fee = 0
    else:
        buyer_fee = ceil_to_cent(ceil_div(fee, 2), policy.decimals)
    seller_fee = fee - buyer_fee
    network_fee = policy.user_network_fee
    seller_receives = amount - seller_fee - network_fee
    if seller_receives < max(policy.min_payout, 1):
        raise AmountError(
            "amount too small: nothing left for the seller after fees",
            amount=amount,
            fee=fee,
            network_fee=network_fee,
        )
    return FeeBreakdown(
        amount=amount,
        fee=fee,
        buyer_fee=buyer_fee,
        seller_fee=seller_fee,
        network_fee=network_fee,
        buyer_pays=amount + buyer_fee,
        seller_receives=seller_receives,
    )


# ── Unique-amount tag ─────────────────────────────────────────────────────────


def tag_step(decimals: int) -> int:
    """Minor units in 0.0001 of the asset."""
    if decimals < TAG_DECIMAL_PLACE:
        raise ValueError("asset precision too low for a 4th-decimal tag")
    return int(10 ** (decimals - TAG_DECIMAL_PLACE))


def tagged_amount(buyer_pays: int, tag_steps: int, decimals: int, *, mode: str) -> tuple[int, int]:
    """Return (amount to pay, tag in minor units). ``discount`` lowers the amount, ``surcharge`` raises it."""
    if not 1 <= tag_steps <= TAG_MAX_STEPS:
        raise ValueError("tag out of range")
    tag = tag_steps * tag_step(decimals)
    if mode == "discount":
        if buyer_pays - tag <= 0:
            raise AmountError("amount too small for a payment tag")
        return buyer_pays - tag, tag
    if mode == "surcharge":
        return buyer_pays + tag, tag
    raise ValueError(f"unknown tag mode {mode!r}")


# ── Settlements ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settlement:
    to_seller: int
    to_buyer: int
    fee_revenue: int
    network_fee_recovered: int

    def __post_init__(self) -> None:
        if min(self.to_seller, self.to_buyer, self.fee_revenue, self.network_fee_recovered) < 0:
            raise AmountError("negative settlement part", settlement=repr(self))

    @property
    def total(self) -> int:
        return self.to_seller + self.to_buyer + self.fee_revenue + self.network_fee_recovered

    @property
    def to_payable(self) -> int:
        return self.to_seller + self.to_buyer


def service_fee_in_escrow(escrow: int, seller_receives: int, network_fee: int) -> int:
    """What the service earns on a normal release: everything in escrow beyond the seller's due
    (absorbs the tag discount and an accepted underpayment, which already lowered ``seller_receives``)."""
    fee = escrow - seller_receives - network_fee
    if fee < 0:
        raise AmountError(
            "escrow does not cover the seller's due", escrow=escrow, seller_receives=seller_receives
        )
    return fee


def settle_release(escrow: int, *, seller_receives: int, network_fee: int) -> Settlement:
    """Buyer confirmed: the seller gets exactly what the card promised."""
    return Settlement(
        to_seller=seller_receives,
        to_buyer=0,
        fee_revenue=service_fee_in_escrow(escrow, seller_receives, network_fee),
        network_fee_recovered=network_fee,
    )


def settle_refund(escrow: int, *, network_fee: int, min_payout: int, service_fee: int = 0) -> Settlement:
    """Money goes back to the buyer (mutual cancel, refund after deadline, late or partial payment).

    The refund network fee is charged only if the buyer still receives at least ``min_payout``;
    otherwise the service absorbs it — we never keep a buyer's money because the refund is small.
    """
    if escrow <= 0:
        raise AmountError("nothing to refund")
    kept = min(max(service_fee, 0), escrow)
    gross = escrow - kept
    charged = network_fee if gross - network_fee >= min_payout else 0
    return Settlement(to_seller=0, to_buyer=gross - charged, fee_revenue=kept, network_fee_recovered=charged)


def settle_dispute(
    escrow: int, *, buyer_share_bps: int, fee_part: int, network_fee: int, min_payout: int
) -> Settlement:
    """Split an escrow by an admin's decision.

    1. ``fee_part`` (the service fee, or 0 when the admin waives it) is taken first;
    2. the rest is split by ``buyer_share_bps`` (buyer's share rounded down);
    3. each side that is paid out bears its own payout network fee;
    4. a share that would be below ``min_payout`` is joined to the other side — one payout instead
       of an unpayable one. The admin sees the final numbers before confirming.
    """
    if not 0 <= buyer_share_bps <= BPS:
        raise ValueError("buyer_share_bps out of range")
    if escrow <= 0:
        raise AmountError("nothing to settle")
    fee_part = min(max(fee_part, 0), escrow)
    net = escrow - fee_part
    buyer_gross = net * buyer_share_bps // BPS
    seller_gross = net - buyer_gross

    def payable(gross: int) -> bool:
        return gross - network_fee >= min_payout

    if buyer_gross and seller_gross:
        if not payable(buyer_gross):
            buyer_gross, seller_gross = 0, net
        elif not payable(seller_gross):
            buyer_gross, seller_gross = net, 0

    fees_charged = 0
    to_buyer, to_seller = buyer_gross, seller_gross
    if to_buyer and payable(to_buyer):
        to_buyer -= network_fee
        fees_charged += network_fee
    if to_seller and payable(to_seller):
        to_seller -= network_fee
        fees_charged += network_fee
    return Settlement(
        to_seller=to_seller, to_buyer=to_buyer, fee_revenue=fee_part, network_fee_recovered=fees_charged
    )
