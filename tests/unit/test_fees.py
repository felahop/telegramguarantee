from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from app.config import ChainsConfig, Settings
from app.domain.enums import FeePayer, Network, NetworkFeePayer
from app.domain.errors import AmountError
from app.domain.fees import (
    TAG_MAX_STEPS,
    FeePolicy,
    calculate_fees,
    service_fee_in_escrow,
    settle_dispute,
    settle_refund,
    settle_release,
    tagged_amount,
)
from app.domain.money import rescale, to_minor

U = 10**6  # 1 USDT with 6 decimals


def policy(
    *,
    decimals: int = 6,
    rate_bps: int = 200,
    min_fee: str = "1",
    network_fee: str = "1.50",
    payer: NetworkFeePayer = NetworkFeePayer.SELLER,
    min_payout: str = "1",
) -> FeePolicy:
    return FeePolicy(
        decimals=decimals,
        rate_bps=rate_bps,
        min_fee=to_minor(min_fee, decimals),
        network_fee=to_minor(network_fee, decimals),
        network_fee_payer=payer,
        min_payout=to_minor(min_payout, decimals),
    )


class TestCardExample:
    """The table from ARCHITECTURE.md §10: 100 USDT, 2 %, min 1, TRC-20 network fee 1.50 on the seller."""

    @pytest.mark.parametrize(
        ("payer", "buyer_pays", "seller_receives"),
        [
            (FeePayer.BUYER, "102.00", "98.50"),
            (FeePayer.SELLER, "100.00", "96.50"),
            (FeePayer.SPLIT, "101.00", "97.50"),
        ],
    )
    def test_table(self, payer: FeePayer, buyer_pays: str, seller_receives: str) -> None:
        b = calculate_fees(100 * U, payer, policy())
        assert b.fee == 2 * U
        assert b.buyer_pays == to_minor(buyer_pays, 6)
        assert b.seller_receives == to_minor(seller_receives, 6)
        assert b.network_fee == 1_500_000
        assert b.buyer_pays - b.seller_receives == b.service_gross


def test_minimum_fee_applies() -> None:
    assert calculate_fees(10 * U, FeePayer.BUYER, policy()).fee == 1 * U  # 2% = 0.20 < 1.00


def test_percent_fee_rounds_up_to_cent() -> None:
    # 2% of 123.45 = 2.469 → 2.47
    assert calculate_fees(123_450_000, FeePayer.SELLER, policy()).fee == 2_470_000


def test_split_gives_odd_cent_to_buyer() -> None:
    b = calculate_fees(to_minor("150.50", 6), FeePayer.SPLIT, policy())  # fee 3.01
    assert b.fee == 3_010_000
    assert b.buyer_fee == 1_510_000
    assert b.seller_fee == 1_500_000


def test_service_pays_network_fee() -> None:
    b = calculate_fees(100 * U, FeePayer.BUYER, policy(payer=NetworkFeePayer.SERVICE))
    assert b.network_fee == 0
    assert b.seller_receives == 100 * U


def test_too_small_after_fees_is_rejected() -> None:
    with pytest.raises(AmountError, match="too small"):
        calculate_fees(2 * U, FeePayer.SELLER, policy())  # 2 - 1 fee - 1.5 network < 1


def test_policy_from_settings_uses_network_decimals(settings: Settings, chains: ChainsConfig) -> None:
    bsc = FeePolicy.from_settings(settings, chains.spec(Network.BSC))
    tron = FeePolicy.from_settings(settings, chains.spec(Network.TRON))
    assert bsc.min_fee == 10**18
    assert tron.min_fee == 10**6


@given(
    amount_cents=st.integers(min_value=500, max_value=10_000_000),
    payer=st.sampled_from(list(FeePayer)),
    rate=st.integers(min_value=0, max_value=1000),
    nf_payer=st.sampled_from(list(NetworkFeePayer)),
)
def test_fee_invariants(amount_cents: int, payer: FeePayer, rate: int, nf_payer: NetworkFeePayer) -> None:
    p = policy(rate_bps=rate, payer=nf_payer)
    amount = amount_cents * 10_000
    try:
        b = calculate_fees(amount, payer, p)
    except AmountError:
        return
    assert b.buyer_pays - b.seller_receives == b.fee + b.network_fee
    assert b.buyer_fee + b.seller_fee == b.fee
    assert b.fee >= p.min_fee
    assert b.fee % 10_000 == 0  # whole cents
    assert b.seller_receives >= p.min_payout
    if payer is FeePayer.SPLIT:
        assert 0 <= b.buyer_fee - b.seller_fee <= 10_000


@given(amount_cents=st.integers(min_value=500, max_value=10_000_000), payer=st.sampled_from(list(FeePayer)))
def test_fees_identical_across_decimals(amount_cents: int, payer: FeePayer) -> None:
    """The same USDT deal on BEP-20 (18 dp) and TRC-20 (6 dp) must cost exactly the same."""
    six = calculate_fees(amount_cents * 10**4, payer, policy(decimals=6))
    eighteen = calculate_fees(amount_cents * 10**16, payer, policy(decimals=18))
    assert rescale(six.fee, 6, 18) == eighteen.fee
    assert rescale(six.buyer_pays, 6, 18) == eighteen.buyer_pays
    assert rescale(six.seller_receives, 6, 18) == eighteen.seller_receives


class TestTag:
    def test_discount_lowers_amount(self) -> None:
        assert tagged_amount(102 * U, 137, 6, mode="discount") == (101_986_300, 13_700)

    def test_surcharge_raises_amount(self) -> None:
        assert tagged_amount(102 * U, 137, 6, mode="surcharge") == (102_013_700, 13_700)

    def test_bep20_precision(self) -> None:
        amount, tag = tagged_amount(102 * 10**18, 137, 18, mode="discount")
        assert tag == 137 * 10**14
        assert amount == to_minor("101.9863", 18)

    @pytest.mark.parametrize("steps", [0, TAG_MAX_STEPS + 1])
    def test_range(self, steps: int) -> None:
        with pytest.raises(ValueError):
            tagged_amount(102 * U, steps, 6, mode="discount")

    def test_max_tag_below_min_fee(self, settings: Settings) -> None:
        # In discount mode the tag is paid out of the fee: it can never exceed the minimum fee.
        _, max_tag = tagged_amount(102 * U, TAG_MAX_STEPS, 6, mode="discount")
        assert max_tag < to_minor(settings.fee_min_usdt, 6)


class TestSettlements:
    def test_release_matches_card(self) -> None:
        escrow = 101_986_300  # 102.00 paid with a 0.0137 discount tag
        s = settle_release(escrow, seller_receives=98_500_000, network_fee=1_500_000)
        assert s.to_seller == 98_500_000
        assert s.fee_revenue == 1_986_300
        assert s.network_fee_recovered == 1_500_000
        assert s.total == escrow

    def test_release_refuses_short_escrow(self) -> None:
        with pytest.raises(AmountError):
            settle_release(90 * U, seller_receives=98 * U, network_fee=0)

    def test_refund_charges_network_fee(self) -> None:
        s = settle_refund(102 * U, network_fee=1_500_000, min_payout=U)
        assert (s.to_buyer, s.fee_revenue, s.network_fee_recovered) == (100_500_000, 0, 1_500_000)

    def test_small_refund_absorbs_network_fee(self) -> None:
        s = settle_refund(2 * U, network_fee=1_500_000, min_payout=U)
        assert s.to_buyer == 2 * U
        assert s.network_fee_recovered == 0

    def test_refund_with_kept_fee(self) -> None:
        s = settle_refund(102 * U, network_fee=0, min_payout=U, service_fee=2 * U)
        assert (s.to_buyer, s.fee_revenue) == (100 * U, 2 * U)

    def test_dispute_seller_equals_release(self) -> None:
        escrow, seller_receives, nf = 101_986_300, 98_500_000, 1_500_000
        fee = service_fee_in_escrow(escrow, seller_receives, nf)
        decided = settle_dispute(escrow, buyer_share_bps=0, fee_part=fee, network_fee=nf, min_payout=U)
        assert decided == settle_release(escrow, seller_receives=seller_receives, network_fee=nf)

    def test_dispute_split_half(self) -> None:
        s = settle_dispute(102 * U, buyer_share_bps=5000, fee_part=2 * U, network_fee=U, min_payout=U)
        assert (s.to_buyer, s.to_seller, s.fee_revenue, s.network_fee_recovered) == (
            49 * U,
            49 * U,
            2 * U,
            2 * U,
        )

    def test_dispute_without_fee(self) -> None:
        s = settle_dispute(102 * U, buyer_share_bps=10_000, fee_part=0, network_fee=0, min_payout=U)
        assert s.to_buyer == 102 * U
        assert s.fee_revenue == 0

    def test_dust_share_joins_other_side(self) -> None:
        # 1% of 100 = 1 USDT, minus 1.5 network fee would be unpayable → whole net to the seller
        s = settle_dispute(100 * U, buyer_share_bps=100, fee_part=0, network_fee=1_500_000, min_payout=U)
        assert s.to_buyer == 0
        assert s.to_seller == 100 * U - 1_500_000
        assert s.total == 100 * U


@given(
    escrow=st.integers(min_value=5 * U, max_value=10**7 * U),
    bps=st.integers(min_value=0, max_value=10_000),
    fee_part=st.integers(min_value=0, max_value=1000 * U),
    nf=st.integers(min_value=0, max_value=5 * U),
    min_payout=st.integers(min_value=1, max_value=5 * U),
)
def test_dispute_settlement_conserves_money(
    escrow: int, bps: int, fee_part: int, nf: int, min_payout: int
) -> None:
    s = settle_dispute(escrow, buyer_share_bps=bps, fee_part=fee_part, network_fee=nf, min_payout=min_payout)
    assert s.total == escrow
    assert s.fee_revenue == min(fee_part, escrow)
    if s.to_buyer and s.to_seller:  # two payouts only when both are actually payable
        assert s.to_buyer >= min_payout
        assert s.to_seller >= min_payout


@given(
    escrow=st.integers(min_value=1, max_value=10**7 * U),
    nf=st.integers(min_value=0, max_value=5 * U),
    fee=st.integers(min_value=0, max_value=10 * U),
)
def test_refund_conserves_money(escrow: int, nf: int, fee: int) -> None:
    assume(escrow > fee)
    s = settle_refund(escrow, network_fee=nf, min_payout=U, service_fee=fee)
    assert s.total == escrow
    assert s.to_buyer > 0
