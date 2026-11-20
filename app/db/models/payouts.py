from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum, updated_at_column
from app.domain.enums import (
    ApprovalDecision,
    Asset,
    Network,
    PayoutAttemptStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
)


class HotWallet(Base):
    """Our signing wallets. ``next_nonce``/``next_seqno`` are allocated under a row lock —
    never taken from ``eth_getTransactionCount('pending')`` of a random public RPC."""

    __tablename__ = "hot_wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    address: Mapped[str]
    next_nonce: Mapped[int | None]
    next_seqno: Mapped[int | None]
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())

    # One EVM key is usually the hot wallet on BSC and on Ethereum: nonces are per network.
    __table_args__ = (sa.UniqueConstraint("network", "address", name="uq_hot_wallets_network_address"),)


class Payout(Base):
    """Outgoing money movement; the table itself is the payout queue (SKIP LOCKED)."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), unique=True)  # also CryptoBot spend_id
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    incoming_transfer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("incoming_transfers.id"))
    kind: Mapped[PayoutKind] = mapped_column(str_enum(PayoutKind, "kind"))
    provider: Mapped[Provider] = mapped_column(str_enum(Provider, "provider"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    asset: Mapped[Asset] = mapped_column(str_enum(Asset, "asset"))
    hot_wallet_id: Mapped[int | None] = mapped_column(sa.ForeignKey("hot_wallets.id"))
    recipient_user_id: Mapped[int | None]
    recipient_address: Mapped[str | None]
    amount: Mapped[int] = mapped_column(MinorAmount)
    status: Mapped[PayoutStatus] = mapped_column(str_enum(PayoutStatus, "status"))
    approvals_required: Mapped[int] = mapped_column(sa.SmallInteger, server_default="0")
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    next_attempt_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    external_id: Mapped[str | None]
    confirmed_tx_hash: Mapped[str | None]
    native_fee_spent: Mapped[int | None] = mapped_column(MinorAmount)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
    confirmed_at: Mapped[datetime | None]

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint(
            "recipient_user_id IS NOT NULL OR recipient_address IS NOT NULL OR status = 'awaiting_address'",
            name="has_recipient",
        ),
        sa.Index(
            "ix_payouts_queue",
            "next_attempt_at",
            postgresql_where=sa.text("status IN ('queued', 'failed_retryable', 'broadcast', 'signing')"),
        ),
    )


class PayoutAttempt(Base):
    """Every signed transaction is stored BEFORE broadcast: a retry re-sends it instead of signing anew."""

    __tablename__ = "payout_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    payout_id: Mapped[int] = mapped_column(sa.ForeignKey("payouts.id"))
    attempt_no: Mapped[int] = mapped_column(sa.Integer)
    tx_hash: Mapped[str | None]
    raw_tx: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    nonce: Mapped[int | None]
    seqno: Mapped[int | None]
    valid_until: Mapped[datetime | None]
    status: Mapped[PayoutAttemptStatus] = mapped_column(str_enum(PayoutAttemptStatus, "status"))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.UniqueConstraint("payout_id", "attempt_no"),
        sa.Index(
            "uq_payout_attempts_tx_hash",
            "tx_hash",
            unique=True,
            postgresql_where=sa.text("tx_hash IS NOT NULL"),
        ),
    )


class PayoutApproval(Base):
    __tablename__ = "payout_approvals"

    payout_id: Mapped[int] = mapped_column(sa.ForeignKey("payouts.id"), primary_key=True)
    admin_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), primary_key=True)
    decision: Mapped[ApprovalDecision] = mapped_column(str_enum(ApprovalDecision, "decision"))
    totp_ok: Mapped[bool]
    created_at: Mapped[datetime] = created_at_column()
