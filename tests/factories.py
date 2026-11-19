"""Low-level row factories for tests that exercise the schema directly (bypassing services)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, PaymentRequest, User
from app.domain.enums import (
    DealSource,
    DealStatus,
    FeePayer,
    Network,
    NetworkFeePayer,
    PartyRole,
    PaymentRequestStatus,
)


async def make_user(session: AsyncSession, user_id: int, username: str | None = None, **kw: Any) -> User:
    user = User(id=user_id, username=username, first_name=username or f"user{user_id}", **kw)
    session.add(user)
    await session.flush()
    return user


async def make_deal_row(
    session: AsyncSession,
    *,
    buyer_id: int,
    seller_id: int,
    status: DealStatus = DealStatus.FUNDED,
    network: Network = Network.TRON,
    amount: int = 100_000_000,
    **kw: Any,
) -> Deal:
    values: dict[str, Any] = {
        "code": secrets.token_hex(4).upper(),
        "status": status,
        "source": DealSource.WIZARD,
        "creator_id": buyer_id,
        "creator_role": PartyRole.BUYER,
        "buyer_id": buyer_id,
        "seller_id": seller_id,
        "terms": "test terms",
        "provider": network.provider,
        "network": network,
        "decimals": 6,
        "amount": amount,
        "fee_payer": FeePayer.BUYER,
        "fee_rate_bps": 200,
        "fee_min": 1_000_000,
        "fee_amount": 2_000_000,
        "network_fee_amount": 0,
        "network_fee_payer": NetworkFeePayer.SELLER,
        "buyer_pays": amount + 2_000_000,
        "seller_receives": amount,
        "execution_hours": 24,
    }
    values.update(kw)
    deal = Deal(**values)
    session.add(deal)
    await session.flush()
    return deal


async def make_payment_request(
    session: AsyncSession, deal: Deal, *, expected: int, tag: int = 0, reserved: bool = True, **kw: Any
) -> PaymentRequest:
    values: dict[str, Any] = {
        "deal_id": deal.id,
        "provider": deal.provider,
        "network": deal.network,
        "asset": deal.asset,
        "expected_amount": expected,
        "tag_amount": tag,
        "deposit_address": "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf",
        "status": PaymentRequestStatus.ACTIVE,
        "tag_reserved": reserved,
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    values.update(kw)
    request = PaymentRequest(**values)
    session.add(request)
    await session.flush()
    return request
