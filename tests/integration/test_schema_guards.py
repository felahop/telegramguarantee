"""The database itself enforces the money invariants (defense in depth)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, DealEvent, IncomingTransfer, Payout
from app.domain.enums import (
    ActorType,
    DealStatus,
    Network,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
    TransferStatus,
)
from tests.factories import make_deal_row, make_payment_request, make_user


async def _parties(session: AsyncSession) -> tuple[int, int]:
    await make_user(session, 1, "buyer")
    await make_user(session, 2, "seller")
    return 1, 2


def _transfer(**kw: object) -> IncomingTransfer:
    values: dict[str, object] = {
        "provider": Provider.ONCHAIN,
        "network": Network.TRON,
        "asset": "USDT",
        "tx_hash": "a" * 64,
        "event_index": 0,
        "to_address": "TXYZ",
        "amount": 100,
        "status": TransferStatus.FINAL,
    }
    values.update(kw)
    return IncomingTransfer(**values)


async def test_self_deal_is_rejected(session: AsyncSession) -> None:
    await make_user(session, 1, "same")
    with pytest.raises(IntegrityError, match="ck_deals_not_self"):
        await make_deal_row(session, buyer_id=1, seller_id=1)


async def test_unknown_enum_value_is_rejected(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller)
    with pytest.raises(IntegrityError, match="ck_deals_status"):
        await session.execute(
            text("UPDATE deals SET status = 'PAID_OUT_LOL' WHERE id = :id"), {"id": deal.id}
        )


async def test_same_onchain_event_cannot_fund_two_deals(session: AsyncSession) -> None:
    session.add(_transfer())
    await session.flush()
    session.add(_transfer(amount=999))
    with pytest.raises(IntegrityError, match="uq_incoming_transfers_event"):
        await session.flush()


async def test_other_log_in_same_tx_is_a_different_event(session: AsyncSession) -> None:
    session.add_all([_transfer(event_index=0), _transfer(event_index=1), _transfer(network=Network.BSC)])
    await session.flush()


async def test_payment_tag_unique_only_while_reserved(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    d1 = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.AWAITING_PAYMENT)
    d2 = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.AWAITING_PAYMENT)
    d3 = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.AWAITING_PAYMENT)
    r1 = await make_payment_request(session, d1, expected=101_986_300, tag=13_700)
    with pytest.raises(IntegrityError, match="uq_payment_requests_tag"):
        async with session.begin_nested():
            await make_payment_request(session, d2, expected=101_986_300, tag=13_700)
    # after the cooldown the tag is released and the amount can be issued again
    r1.tag_reserved = False
    r1.status = PaymentRequestStatus.EXPIRED
    await session.flush()
    await make_payment_request(session, d3, expected=101_986_300, tag=13_700)


async def test_one_active_payment_request_per_deal(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.AWAITING_PAYMENT)
    await make_payment_request(session, deal, expected=100)
    with pytest.raises(IntegrityError, match="uq_payment_requests_one_active"):
        await make_payment_request(session, deal, expected=101)


async def test_audit_log_is_append_only(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller)
    event = DealEvent(
        deal_id=deal.id,
        seq=1,
        actor_type=ActorType.SYSTEM,
        action="x",
        hash=b"h",
        created_at=deal.created_at,
    )
    session.add(event)
    await session.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        async with session.begin_nested():
            await session.execute(update(DealEvent).where(DealEvent.id == event.id).values(action="forged"))
    with pytest.raises(DBAPIError, match="append-only"):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM deal_events"))
    with pytest.raises(DBAPIError, match="append-only"):
        async with session.begin_nested():
            await session.execute(text("TRUNCATE deal_events CASCADE"))


def _payout(deal: Deal, kind: PayoutKind, **kw: object) -> Payout:
    values: dict[str, object] = {
        "idempotency_key": f"{kind}:{deal.id}:1",
        "deal_id": deal.id,
        "kind": kind,
        "provider": deal.provider,
        "network": deal.network,
        "asset": deal.asset,
        "recipient_address": "TSellerAddress",
        "amount": 98_000_000,
        "status": PayoutStatus.QUEUED,
    }
    values.update(kw)
    return Payout(**values)


@pytest.mark.parametrize(
    "status",
    [DealStatus.FUNDED, DealStatus.SELLER_CONFIRMED, DealStatus.DISPUTED, DealStatus.PAYOUT_PENDING],
)
async def test_seller_payout_requires_buyer_confirmation_or_decision(
    session: AsyncSession, status: DealStatus
) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=status)
    session.add(_payout(deal, PayoutKind.SELLER_PAYOUT))
    with pytest.raises(IntegrityError, match="seller-bound payout"):
        await session.flush()


@pytest.mark.parametrize("status", [DealStatus.BUYER_CONFIRMED, DealStatus.RESOLVED_SELLER, DealStatus.SPLIT])
async def test_seller_payout_allowed_after_confirmation(session: AsyncSession, status: DealStatus) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=status)
    kind = PayoutKind.SPLIT_SELLER if status is DealStatus.SPLIT else PayoutKind.SELLER_PAYOUT
    session.add(_payout(deal, kind))
    await session.flush()


async def test_refund_payout_not_restricted(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.FUNDED)
    session.add(
        _payout(deal, PayoutKind.BUYER_REFUND, recipient_address=None, status=PayoutStatus.AWAITING_ADDRESS)
    )
    await session.flush()


async def test_payout_recipient_is_immutable(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.BUYER_CONFIRMED)
    payout = _payout(deal, PayoutKind.SELLER_PAYOUT)
    session.add(payout)
    await session.flush()
    for change in ({"recipient_address": "TAttacker"}, {"amount": 1}, {"kind": PayoutKind.BUYER_REFUND}):
        with pytest.raises(IntegrityError, match="immutable"):
            async with session.begin_nested():
                await session.execute(update(Payout).where(Payout.id == payout.id).values(**change))
    # status transitions are of course allowed
    await session.execute(update(Payout).where(Payout.id == payout.id).values(status=PayoutStatus.SIGNING))


async def test_refund_address_can_be_filled_once(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller, status=DealStatus.REFUND_PENDING)
    payout = _payout(
        deal, PayoutKind.BUYER_REFUND, recipient_address=None, status=PayoutStatus.AWAITING_ADDRESS
    )
    session.add(payout)
    await session.flush()
    await session.execute(
        update(Payout)
        .where(Payout.id == payout.id)
        .values(recipient_address="TBuyer", status=PayoutStatus.QUEUED)
    )
    with pytest.raises(IntegrityError, match="immutable"):
        await session.execute(update(Payout).where(Payout.id == payout.id).values(recipient_address="TOther"))


async def test_amount_column_rejects_float(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    deal = await make_deal_row(session, buyer_id=buyer, seller_id=seller)
    deal.amount = 1.5  # type: ignore[assignment]
    with pytest.raises(Exception, match="minor amounts must be int"):
        await session.flush()


async def test_minor_amount_round_trips_uint256(session: AsyncSession) -> None:
    buyer, seller = await _parties(session)
    big = 2**256 - 1
    deal = await make_deal_row(
        session, buyer_id=buyer, seller_id=seller, amount=big, buyer_pays=big, seller_receives=big
    )
    deal_id = deal.id
    await session.commit()
    session.expire_all()
    loaded = (await session.execute(select(Deal).where(Deal.id == deal_id))).scalar_one()
    assert loaded.amount == big
    assert isinstance(loaded.amount, int)
