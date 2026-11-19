from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.bot.callbacks import DealCb
from app.bot.chat_filters import scan
from app.bot.keyboards import execution_rows
from app.db.models import Deal
from app.domain.enums import DealStatus, FeePayer, Network, NetworkFeePayer, PartyRole, Provider
from tests import addresses

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
BUYER, SELLER = 1, 2


def deal(status: DealStatus, **kw: object) -> Deal:
    values: dict[str, object] = {
        "id": 5,
        "code": "ABCD2345",
        "version": 1,
        "status": status,
        "creator_id": BUYER,
        "creator_role": PartyRole.BUYER,
        "buyer_id": BUYER,
        "seller_id": SELLER,
        "terms": "t",
        "provider": Provider.CRYPTOBOT,
        "network": Network.CRYPTOBOT,
        "decimals": 6,
        "amount": 1,
        "fee_payer": FeePayer.BUYER,
        "fee_rate_bps": 200,
        "fee_min": 1,
        "fee_amount": 1,
        "network_fee_amount": 0,
        "network_fee_payer": NetworkFeePayer.SELLER,
        "buyer_pays": 2,
        "seller_receives": 1,
        "execution_hours": 24,
        "execution_deadline_at": NOW + timedelta(hours=1),
        "refund_requested_at": None,
        "cancel_req_buyer_at": None,
        "cancel_req_seller_at": None,
    }
    values.update(kw)
    return Deal(**values)


def actions(d: Deal, viewer: int, **kw: object) -> set[str]:
    rows = execution_rows(d, viewer, now=NOW, **kw)  # type: ignore[arg-type]
    return {DealCb.unpack(b.callback_data).a.value for row in rows for b in row if b.callback_data}


class TestExecutionButtons:
    def test_funded(self) -> None:
        d = deal(DealStatus.FUNDED)
        assert actions(d, SELLER) == {"sd", "rc", "dp", "ch"}
        assert actions(d, BUYER) == {"rc", "dp", "ch"}  # no refund request before the deadline

    def test_refund_request_after_deadline(self) -> None:
        d = deal(DealStatus.FUNDED, execution_deadline_at=NOW - timedelta(minutes=1))
        assert "rr" in actions(d, BUYER)
        requested = deal(
            DealStatus.FUNDED, execution_deadline_at=NOW - timedelta(hours=1), refund_requested_at=NOW
        )
        assert "rr" not in actions(requested, BUYER)
        assert {"ar", "or", "sd"} <= actions(requested, SELLER)

    def test_receipt_only_for_buyer_after_seller(self) -> None:
        assert "bg" in actions(deal(DealStatus.SELLER_CONFIRMED), BUYER)
        assert "bg" not in actions(deal(DealStatus.SELLER_CONFIRMED), SELLER)
        assert "bg" not in actions(deal(DealStatus.FUNDED), BUYER)

    def test_cancel_proposal_states(self) -> None:
        proposed = deal(DealStatus.FUNDED, cancel_req_buyer_at=NOW)
        assert "rv" in actions(proposed, BUYER)
        assert "rc" in actions(proposed, SELLER)  # «Согласиться на отмену»

    def test_dispute_withdrawal_and_refund_address(self) -> None:
        assert "wd" in actions(deal(DealStatus.DISPUTED), BUYER, can_withdraw_dispute=True)
        assert "wd" not in actions(deal(DealStatus.DISPUTED), SELLER)
        assert actions(deal(DealStatus.REFUND_PENDING), BUYER, needs_refund_address=True) == {"ra"}

    def test_strangers_get_nothing(self) -> None:
        assert actions(deal(DealStatus.FUNDED), 999) == set()

    @pytest.mark.parametrize("status", [DealStatus.COMPLETED, DealStatus.PAYOUT_PENDING, DealStatus.REFUNDED])
    def test_closed_deals_have_no_actions(self, status: DealStatus) -> None:
        assert actions(deal(status), BUYER) == set()


class TestChatFilters:
    @pytest.mark.parametrize(
        ("text", "flags"),
        [
            ("Привет, когда будет готово?", []),
            ("карта 4111 1111 1111 1111", ["card"]),
            ("номер заказа 1234 5678 9012 3456", []),  # fails the Luhn check
            (f"кидай на {addresses.tron(1)}", ["crypto_address"]),
            (f"адрес {addresses.evm(1)}", ["crypto_address"]),
            (f"TON: {addresses.ton(1)}", ["crypto_address"]),
            ("смотри https://example.com/pay", ["link"]),
            ("пиши в t.me/fake_garant", ["link"]),
            ("это @real_garant_bot, через него дешевле", ["mention"]),
            ("почта alice@example.com", []),
        ],
    )
    def test_scan(self, text: str, flags: list[str]) -> None:
        assert scan(text) == flags

    def test_empty(self) -> None:
        assert scan(None) == []
