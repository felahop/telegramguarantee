from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum, updated_at_column
from app.domain.enums import (
    ActorType,
    Asset,
    DealResolution,
    DealSource,
    DealStatus,
    DisputeDecision,
    DisputeReason,
    DisputeStatus,
    FeePayer,
    Network,
    NetworkFeePayer,
    PartyRole,
    Provider,
    TimerKind,
)


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(16), unique=True)  # human code, TON comment
    invite_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    version: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    event_seq: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    last_event_hash: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    status: Mapped[DealStatus] = mapped_column(
        str_enum(DealStatus, "status"), server_default=DealStatus.CREATED.value
    )
    status_before_dispute: Mapped[DealStatus | None] = mapped_column(
        str_enum(DealStatus, "status_before_dispute")
    )
    resolution: Mapped[DealResolution | None] = mapped_column(str_enum(DealResolution, "resolution"))
    source: Mapped[DealSource] = mapped_column(str_enum(DealSource, "source"))

    creator_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))
    creator_role: Mapped[PartyRole] = mapped_column(str_enum(PartyRole, "creator_role"))
    buyer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"))
    seller_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"))
    invited_user_id: Mapped[int | None]
    invited_username: Mapped[str | None]
    # Someone opened an open invite link; the creator must confirm them before ACCEPTED.
    pending_counterparty_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"))

    terms: Mapped[str]
    provider: Mapped[Provider] = mapped_column(str_enum(Provider, "provider"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    asset: Mapped[Asset] = mapped_column(str_enum(Asset, "asset"), server_default=Asset.USDT.value)
    decimals: Mapped[int] = mapped_column(sa.SmallInteger)

    amount: Mapped[int] = mapped_column(MinorAmount)
    fee_payer: Mapped[FeePayer] = mapped_column(str_enum(FeePayer, "fee_payer"))
    fee_rate_bps: Mapped[int] = mapped_column(sa.Integer)
    fee_min: Mapped[int] = mapped_column(MinorAmount)
    fee_amount: Mapped[int] = mapped_column(MinorAmount)
    network_fee_amount: Mapped[int] = mapped_column(MinorAmount, server_default="0")
    network_fee_payer: Mapped[NetworkFeePayer] = mapped_column(str_enum(NetworkFeePayer, "network_fee_payer"))
    buyer_pays: Mapped[int] = mapped_column(MinorAmount)
    seller_receives: Mapped[int] = mapped_column(MinorAmount)
    received_amount: Mapped[int] = mapped_column(MinorAmount, server_default="0")

    execution_hours: Mapped[int] = mapped_column(sa.Integer)
    execution_deadline_at: Mapped[datetime | None]
    deadline_notified_at: Mapped[datetime | None]
    payment_expires_at: Mapped[datetime | None]
    payment_extended: Mapped[bool] = mapped_column(server_default=sa.false())

    seller_payout_address: Mapped[str | None]
    buyer_refund_address: Mapped[str | None]

    next_action_at: Mapped[datetime | None]
    next_action_kind: Mapped[TimerKind | None] = mapped_column(str_enum(TimerKind, "next_action_kind"))
    reminders_sent: Mapped[int] = mapped_column(sa.SmallInteger, server_default="0")

    cancel_req_buyer_at: Mapped[datetime | None]
    cancel_req_seller_at: Mapped[datetime | None]
    refund_requested_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = created_at_column()
    accepted_at: Mapped[datetime | None]
    funded_at: Mapped[datetime | None]
    seller_confirmed_at: Mapped[datetime | None]
    # Start of the buyer-silence clock (reminders, auto-dispute); restarts when a dispute is withdrawn.
    confirm_wait_started_at: Mapped[datetime | None]
    buyer_confirmed_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        sa.CheckConstraint("buyer_id IS NULL OR seller_id IS NULL OR buyer_id <> seller_id", name="not_self"),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint("seller_receives > 0", name="seller_receives_positive"),
        sa.CheckConstraint("buyer_pays >= amount", name="buyer_pays_covers_amount"),
        sa.CheckConstraint("received_amount >= 0", name="received_non_negative"),
        sa.CheckConstraint("execution_hours > 0", name="execution_hours_positive"),
        sa.CheckConstraint("char_length(terms) BETWEEN 3 AND 4000", name="terms_length"),
        sa.Index(
            "ix_deals_next_action_at",
            "next_action_at",
            postgresql_where=sa.text("next_action_at IS NOT NULL"),
        ),
        sa.Index("ix_deals_buyer_created", "buyer_id", sa.text("created_at DESC")),
        sa.Index("ix_deals_seller_created", "seller_id", sa.text("created_at DESC")),
        sa.Index("ix_deals_creator_created", "creator_id", sa.text("created_at DESC")),
        sa.Index("ix_deals_status", "status"),
    )

    def party_role(self, user_id: int) -> PartyRole | None:
        if user_id == self.buyer_id:
            return PartyRole.BUYER
        if user_id == self.seller_id:
            return PartyRole.SELLER
        return None

    def counterparty_of(self, user_id: int) -> int | None:
        if user_id == self.buyer_id:
            return self.seller_id
        if user_id == self.seller_id:
            return self.buyer_id
        return None


class DealTemplate(Base):
    __tablename__ = "deal_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    name: Mapped[str]
    role: Mapped[PartyRole] = mapped_column(str_enum(PartyRole, "role"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    amount: Mapped[int | None] = mapped_column(MinorAmount)
    terms: Mapped[str | None]
    execution_hours: Mapped[int | None] = mapped_column(sa.Integer)
    fee_payer: Mapped[FeePayer | None] = mapped_column(str_enum(FeePayer, "fee_payer"))
    created_at: Mapped[datetime] = created_at_column()


class DealEvent(Base):
    """Append-only audit log (UPDATE/DELETE are rejected by a trigger) with a per-deal hash chain."""

    __tablename__ = "deal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(sa.ForeignKey("deals.id"))
    seq: Mapped[int] = mapped_column(sa.Integer)
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type"))
    actor_id: Mapped[int | None]
    action: Mapped[str] = mapped_column(sa.String(64))
    from_status: Mapped[DealStatus | None] = mapped_column(str_enum(DealStatus, "from_status"))
    to_status: Mapped[DealStatus | None] = mapped_column(str_enum(DealStatus, "to_status"))
    tx_hash: Mapped[str | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    prev_hash: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    hash: Mapped[bytes] = mapped_column(sa.LargeBinary)
    created_at: Mapped[datetime]

    __table_args__ = (sa.UniqueConstraint("deal_id", "seq"),)


class DealMessage(Base):
    __tablename__ = "deal_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    sender_id: Mapped[int]
    sender_role: Mapped[str] = mapped_column(sa.String(16))
    text: Mapped[str | None]
    file_id: Mapped[str | None]
    file_type: Mapped[str | None] = mapped_column(sa.String(16))
    flagged: Mapped[list[str]] = mapped_column(sa.ARRAY(sa.Text), server_default="{}")
    created_at: Mapped[datetime] = created_at_column()


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(sa.ForeignKey("deals.id"))
    opened_by: Mapped[int | None]  # NULL = opened by the system
    reason: Mapped[DisputeReason] = mapped_column(str_enum(DisputeReason, "reason"))
    reason_text: Mapped[str | None]
    status: Mapped[DisputeStatus] = mapped_column(
        str_enum(DisputeStatus, "status"), server_default=DisputeStatus.OPEN.value
    )
    arbitrator_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"))
    decision: Mapped[DisputeDecision | None] = mapped_column(str_enum(DisputeDecision, "decision"))
    buyer_share_bps: Mapped[int | None] = mapped_column(sa.Integer)
    keep_fee: Mapped[bool | None]
    comment: Mapped[str | None]
    sla_alerted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()
    resolved_at: Mapped[datetime | None]

    __table_args__ = (
        sa.CheckConstraint("buyer_share_bps IS NULL OR buyer_share_bps BETWEEN 0 AND 10000", name="share"),
        sa.Index(
            "uq_disputes_one_open",
            "deal_id",
            unique=True,
            postgresql_where=sa.text("status IN ('open', 'in_review')"),
        ),
    )
