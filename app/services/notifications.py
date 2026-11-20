"""Notification outbox writer.

Notifications are rows written in the same DB transaction as the change they describe; a worker
delivers them to Telegram later (retries, rate limits, 403 handling). A crash can therefore never
produce "status changed but nobody was told" or "told but the change rolled back".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationOutbox, User
from app.domain.enums import UserRole


class Notice(StrEnum):
    DEAL_OFFER = "deal_offer"
    DEAL_EDITED = "deal_edited"
    CONFIRM_COUNTERPARTY = "confirm_counterparty"
    COUNTERPARTY_REJECTED = "counterparty_rejected"
    DEAL_ACCEPTED = "deal_accepted"
    PAYMENT_STARTED = "payment_started"
    DEAL_FUNDED = "deal_funded"
    SELLER_CONFIRMED = "seller_confirmed"
    PAYOUT_STARTED = "payout_started"
    DEAL_COMPLETED = "deal_completed"
    DEAL_CANCELLED = "deal_cancelled"
    DEAL_EXPIRED = "deal_expired"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_REVOKED = "cancel_revoked"
    REFUND_REQUESTED = "refund_requested"
    REFUND_STARTED = "refund_started"
    REFUND_ADDRESS_NEEDED = "refund_address_needed"
    DEAL_REFUNDED = "deal_refunded"
    DEADLINE_PASSED = "deadline_passed"
    BUYER_REMINDER = "buyer_reminder"
    PAYOUT_ADDRESS_CHANGED = "payout_address_changed"
    DISPUTE_OPENED = "dispute_opened"
    DISPUTE_WITHDRAWN = "dispute_withdrawn"
    DISPUTE_RESOLVED = "dispute_resolved"
    DISPUTE_IN_REVIEW = "dispute_in_review"
    PAYOUT_ACTION_NEEDED = "payout_action_needed"  # e.g. the recipient never opened @CryptoBot
    INLINE_CARD = "inline_card"  # edit the inline-mode card the creator posted in a chat
    ADMIN_DISPUTE_OPENED = "admin_dispute_opened"
    ADMIN_DISPUTE_SLA = "admin_dispute_sla"
    ADMIN_PAYOUT_APPROVAL = "admin_payout_approval"
    ADMIN_PAYOUT_FAILED = "admin_payout_failed"
    ADMIN_PAYMENT_ANOMALY = "admin_payment_anomaly"


ADMIN_NOTICES = frozenset(
    {
        Notice.ADMIN_DISPUTE_OPENED,
        Notice.ADMIN_DISPUTE_SLA,
        Notice.ADMIN_PAYOUT_APPROVAL,
        Notice.ADMIN_PAYOUT_FAILED,
        Notice.ADMIN_PAYMENT_ANOMALY,
    }
)


async def enqueue(
    session: AsyncSession,
    *,
    user_id: int,
    kind: Notice,
    dedupe_key: str,
    now: datetime,
    deal_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """``now`` comes from the caller's clock (the sender compares against the same clock)."""
    await session.execute(
        pg_insert(NotificationOutbox)
        .values(
            user_id=user_id,
            deal_id=deal_id,
            kind=kind.value,
            dedupe_key=dedupe_key,
            payload=payload or {},
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[NotificationOutbox.dedupe_key])
    )


async def admin_ids(session: AsyncSession) -> list[int]:
    rows = await session.execute(
        sa.select(User.id).where(
            User.role.in_([UserRole.ADMIN, UserRole.ARBITRATOR]), User.is_banned.is_(False)
        )
    )
    return list(rows.scalars())
