"""Deal timers: the single next action due for a deal, derived from its state.

The deal row stores only ``next_action_at``/``next_action_kind``; after every change the service
recomputes them with :func:`next_timer`. A worker picks due deals with ``FOR UPDATE SKIP LOCKED``
— no per-deal scheduled jobs that could be lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.config import Settings
from app.domain.enums import DealStatus, TimerKind


class TimerSubject(Protocol):
    status: DealStatus
    created_at: datetime
    accepted_at: datetime | None
    payment_expires_at: datetime | None
    execution_deadline_at: datetime | None
    deadline_notified_at: datetime | None
    refund_requested_at: datetime | None
    confirm_wait_started_at: datetime | None
    reminders_sent: int


class DisputeTimerSubject(Protocol):
    created_at: datetime
    sla_alerted_at: datetime | None


@dataclass(frozen=True)
class Timer:
    at: datetime
    kind: TimerKind


def next_timer(  # noqa: PLR0911 - one return per deal status
    deal: TimerSubject, settings: Settings, dispute: DisputeTimerSubject | None = None
) -> Timer | None:
    status = deal.status
    if status is DealStatus.CREATED:
        return Timer(deal.created_at + settings.offer_ttl, TimerKind.OFFER_EXPIRE)
    if status is DealStatus.ACCEPTED and deal.accepted_at is not None:
        return Timer(deal.accepted_at + settings.pay_start_ttl, TimerKind.PAY_START_EXPIRE)
    if status is DealStatus.AWAITING_PAYMENT and deal.payment_expires_at is not None:
        return Timer(deal.payment_expires_at, TimerKind.PAYMENT_EXPIRE)
    if status is DealStatus.FUNDED:
        if deal.refund_requested_at is not None:
            return Timer(
                deal.refund_requested_at + settings.refund_objection_window, TimerKind.REFUND_WINDOW_ELAPSED
            )
        if deal.execution_deadline_at is not None and deal.deadline_notified_at is None:
            return Timer(deal.execution_deadline_at, TimerKind.EXECUTION_DEADLINE)
        return None
    if status is DealStatus.SELLER_CONFIRMED and deal.confirm_wait_started_at is not None:
        base = deal.confirm_wait_started_at
        dispute_at = base + settings.buyer_silence_dispute_after
        reminder_at = base + settings.buyer_reminder_interval * (deal.reminders_sent + 1)
        if reminder_at < dispute_at:
            return Timer(reminder_at, TimerKind.BUYER_REMINDER)
        return Timer(dispute_at, TimerKind.AUTO_DISPUTE)
    if status is DealStatus.DISPUTED and dispute is not None and dispute.sla_alerted_at is None:
        return Timer(dispute.created_at + settings.dispute_sla, TimerKind.DISPUTE_SLA)
    return None


# While a matching transfer is visible but not final yet, the payment window is not closed.
PAYMENT_EXPIRY_DEFERRAL = timedelta(minutes=5)
