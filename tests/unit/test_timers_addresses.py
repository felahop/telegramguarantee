from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.chains.addresses import InvalidAddress, validate_address
from app.config import Settings
from app.domain.enums import DealStatus, Network, TimerKind
from app.domain.timers import next_timer
from tests import addresses

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


@dataclass
class FakeDeal:
    status: DealStatus
    created_at: datetime = T0
    accepted_at: datetime | None = None
    payment_expires_at: datetime | None = None
    execution_deadline_at: datetime | None = None
    deadline_notified_at: datetime | None = None
    refund_requested_at: datetime | None = None
    confirm_wait_started_at: datetime | None = None
    reminders_sent: int = 0


@dataclass
class FakeDispute:
    created_at: datetime
    sla_alerted_at: datetime | None = None


class TestTimers:
    def test_offer_and_pay_start(self, settings: Settings) -> None:
        assert next_timer(FakeDeal(DealStatus.CREATED), settings).kind is TimerKind.OFFER_EXPIRE  # type: ignore[union-attr]
        t = next_timer(FakeDeal(DealStatus.ACCEPTED, accepted_at=T0), settings)
        assert t is not None
        assert (t.at, t.kind) == (T0 + settings.pay_start_ttl, TimerKind.PAY_START_EXPIRE)

    def test_payment(self, settings: Settings) -> None:
        t = next_timer(FakeDeal(DealStatus.AWAITING_PAYMENT, payment_expires_at=T0), settings)
        assert t is not None
        assert t.kind is TimerKind.PAYMENT_EXPIRE

    def test_funded_deadline_then_nothing(self, settings: Settings) -> None:
        deal = FakeDeal(DealStatus.FUNDED, execution_deadline_at=T0)
        assert next_timer(deal, settings).kind is TimerKind.EXECUTION_DEADLINE  # type: ignore[union-attr]
        deal.deadline_notified_at = T0
        assert next_timer(deal, settings) is None  # no automatic action: money never moves by itself

    def test_refund_request_window(self, settings: Settings) -> None:
        deal = FakeDeal(DealStatus.FUNDED, execution_deadline_at=T0, refund_requested_at=T0)
        t = next_timer(deal, settings)
        assert t is not None
        assert (t.at, t.kind) == (T0 + settings.refund_objection_window, TimerKind.REFUND_WINDOW_ELAPSED)

    def test_buyer_silence_schedule(self, settings: Settings) -> None:
        deal = FakeDeal(DealStatus.SELLER_CONFIRMED, confirm_wait_started_at=T0)
        schedule = []
        for sent in range(3):
            deal.reminders_sent = sent
            t = next_timer(deal, settings)
            assert t is not None
            schedule.append((t.at - T0, t.kind))
        assert schedule == [
            (timedelta(hours=24), TimerKind.BUYER_REMINDER),
            (timedelta(hours=48), TimerKind.BUYER_REMINDER),
            (timedelta(hours=72), TimerKind.AUTO_DISPUTE),
        ]

    def test_dispute_sla_once(self, settings: Settings) -> None:
        deal = FakeDeal(DealStatus.DISPUTED)
        assert next_timer(deal, settings, FakeDispute(T0)).kind is TimerKind.DISPUTE_SLA  # type: ignore[union-attr]
        assert next_timer(deal, settings, FakeDispute(T0, sla_alerted_at=T0)) is None

    @pytest.mark.parametrize("status", [DealStatus.COMPLETED, DealStatus.REFUNDED, DealStatus.PAYOUT_PENDING])
    def test_no_timer(self, settings: Settings, status: DealStatus) -> None:
        assert next_timer(FakeDeal(status), settings) is None


class TestAddresses:
    def test_evm_checksum(self) -> None:
        good = addresses.evm(1)
        assert validate_address(Network.BSC, good) == good
        assert validate_address(Network.ETH, good.lower()) == good  # lowercase is accepted and normalized

    @pytest.mark.parametrize("bad", ["0x123", "1x" + "a" * 40, "0x" + "0" * 40, ""])
    def test_evm_invalid(self, bad: str) -> None:
        with pytest.raises(InvalidAddress):
            validate_address(Network.ETH, bad)

    def test_evm_mixed_case_typo(self) -> None:
        good = addresses.evm(2)
        # flip the case of one letter: still hex, but the EIP-55 checksum no longer matches
        idx = next(i for i, ch in enumerate(good) if i > 1 and ch.isalpha())
        typo = good[:idx] + good[idx].swapcase() + good[idx + 1 :]
        with pytest.raises(InvalidAddress, match="checksum"):
            validate_address(Network.ETH, typo)

    def test_tron(self) -> None:
        good = addresses.tron(1)
        assert validate_address(Network.TRON, good) == good
        typo = good[:-1] + ("1" if good[-1] != "1" else "2")
        with pytest.raises(InvalidAddress):
            validate_address(Network.TRON, typo)
        with pytest.raises(InvalidAddress):
            validate_address(Network.TRON, addresses.sol(1))

    def test_ton(self) -> None:
        good = addresses.ton(1)
        assert validate_address(Network.TON, good) == good
        assert validate_address(Network.TON, addresses.ton(1, bounceable=False))
        typo = good[:10] + ("A" if good[10] != "A" else "B") + good[11:]
        with pytest.raises(InvalidAddress):
            validate_address(Network.TON, typo)
        testnet = addresses.ton(1, testnet=True)
        with pytest.raises(InvalidAddress, match="testnet"):
            validate_address(Network.TON, testnet)
        assert validate_address(Network.TON, testnet, allow_testnet=True) == testnet
        raw = "0:" + "ab" * 32
        assert validate_address(Network.TON, raw) == raw

    def test_solana(self) -> None:
        good = addresses.sol(1)
        assert validate_address(Network.SOL, good) == good
        with pytest.raises(InvalidAddress):
            validate_address(Network.SOL, "1111")
        with pytest.raises(InvalidAddress):
            validate_address(Network.SOL, "0OIl" * 11)  # not base58

    def test_cryptobot_has_no_address(self) -> None:
        with pytest.raises(InvalidAddress):
            validate_address(Network.CRYPTOBOT, "anything")
