from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.config import ChainsConfig
from app.domain.enums import Network
from app.domain.errors import AmountError
from app.domain.money import (
    asset_code,
    ceil_to_cent,
    format_amount,
    from_minor,
    parse_user_amount,
    rescale,
    to_minor,
)


class TestToMinor:
    def test_converts_exactly(self) -> None:
        assert to_minor("150", 6) == 150_000_000
        assert to_minor(Decimal("150.000137"), 6) == 150_000_137
        assert to_minor("150.000137", 18) == 150_000_137_000_000_000_000

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            to_minor(0.1, 6)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        with pytest.raises(TypeError):
            to_minor(True, 6)  # type: ignore[arg-type]

    def test_never_rounds_silently(self) -> None:
        with pytest.raises(AmountError):
            to_minor("1.0000001", 6)

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "abc", ""])
    def test_rejects_non_numbers(self, bad: str) -> None:
        with pytest.raises(AmountError):
            to_minor(bad, 6)

    @given(st.integers(min_value=0, max_value=10**30), st.integers(min_value=2, max_value=18))
    def test_round_trip(self, amount: int, decimals: int) -> None:
        assert to_minor(from_minor(amount, decimals), decimals) == amount


class TestDecimalsPerNetwork:
    """USDT is 18 decimals on BSC and 6 everywhere else; mixing them up is a 10^12 error."""

    @pytest.mark.parametrize(
        ("network", "decimals"),
        [
            (Network.BSC, 18),
            (Network.ETH, 6),
            (Network.TRON, 6),
            (Network.TON, 6),
            (Network.SOL, 6),
            (Network.CRYPTOBOT, 6),
        ],
    )
    def test_configured_decimals(self, chains: ChainsConfig, network: Network, decimals: int) -> None:
        assert chains.spec(network).decimals == decimals

    def test_same_usdt_amount_differs_in_minor_units(self, chains: ChainsConfig) -> None:
        bsc = chains.spec(Network.BSC).minor("150.25")
        tron = chains.spec(Network.TRON).minor("150.25")
        assert bsc == 150_250_000_000_000_000_000
        assert tron == 150_250_000
        assert rescale(tron, 6, 18) == bsc
        assert rescale(bsc, 18, 6) == tron

    def test_lossy_rescale_is_an_error(self) -> None:
        with pytest.raises(AmountError):
            rescale(1, 18, 6)

    def test_whitelisted_contracts(self, chains: ChainsConfig) -> None:
        assert chains.spec(Network.BSC).contract == "0x55d398326f99059fF775485246999027B3197955"
        assert chains.spec(Network.ETH).contract == "0xdAC17F958D2ee523a2206206994597C13D831ec7"
        assert chains.spec(Network.TRON).contract == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class TestFormatting:
    def test_shows_cents_by_default(self) -> None:
        assert format_amount(150_000_000, 6) == "150.00"

    def test_never_hides_significant_digits(self) -> None:
        # A tagged amount must be shown exactly — the buyer has to send precisely this.
        assert format_amount(101_986_300, 6) == "101.9863"
        assert format_amount(to_minor("101.9863", 18), 18) == "101.9863"

    def test_explicit_places_truncate(self) -> None:
        assert format_amount(98_505_000, 6, places=2) == "98.50"

    def test_ceil_to_cent(self) -> None:
        assert ceil_to_cent(2_000_001, 6) == 2_010_000
        assert ceil_to_cent(2_000_000, 6) == 2_000_000


class TestParseUserAmount:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("150", Decimal("150")),
            ("150.5", Decimal("150.5")),
            ("150,50", Decimal("150.50")),
            (" 1 000 ", Decimal("1000")),
            ("1 000.25", Decimal("1000.25")),
        ],
    )
    def test_accepts(self, text: str, expected: Decimal) -> None:
        assert parse_user_amount(text) == expected

    @pytest.mark.parametrize("text", ["-5", "0", "1e3", "1.234", "abc", "", "1,000.50,1", "١٢٣"])
    def test_rejects(self, text: str) -> None:
        with pytest.raises(AmountError):
            parse_user_amount(text)


def test_asset_code_is_network_specific() -> None:
    assert asset_code("USDT", Network.TRON) == "USDT@tron"
    assert asset_code("USDT", "bsc") == "USDT@bsc"


def test_uint256_scale_values_are_exact() -> None:
    # Above the default 28-digit Decimal precision: must not be rounded.
    big = 2**256 - 1
    assert to_minor(from_minor(big, 18), 18) == big
    assert format_amount(10**40 + 1, 18) == "10000000000000000000000.000000000000000001"
