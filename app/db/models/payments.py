from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum, updated_at_column
from app.domain.enums import (
    Asset,
    DepositAddressStatus,
    MatchMethod,
    MatchStatus,
    Network,
    PaymentRequestStatus,
    Provider,
    TransferStatus,
    TxClaimStatus,
)


class DepositAddress(Base):
    """Per-deal deposit addresses (``deposit_mode=per_deal``). Never reused, even after sweeping."""

    __tablename__ = "deposit_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    address: Mapped[str]
    derivation_index: Mapped[int] = mapped_column(sa.Integer)
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"))
    status: Mapped[DepositAddressStatus] = mapped_column(
        str_enum(DepositAddressStatus, "status"), server_default=DepositAddressStatus.ASSIGNED.value
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.UniqueConstraint("network", "address"),
        sa.UniqueConstraint("network", "derivation_index"),
    )


class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    provider: Mapped[Provider] = mapped_column(str_enum(Provider, "provider"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    asset: Mapped[Asset] = mapped_column(str_enum(Asset, "asset"))
    expected_amount: Mapped[int] = mapped_column(MinorAmount)  # what must arrive, tag included
    tag_amount: Mapped[int] = mapped_column(MinorAmount, server_default="0")
    deposit_address: Mapped[str | None]
    deposit_address_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deposit_addresses.id"))
    memo: Mapped[str | None]
    sol_reference: Mapped[str | None]
    external_id: Mapped[str | None]  # CryptoBot invoice_id
    pay_url: Mapped[str | None]
    fx_rate: Mapped[Decimal | None] = mapped_column(sa.Numeric(38, 18))
    fx_source: Mapped[str | None]
    fx_at: Mapped[datetime | None]
    status: Mapped[PaymentRequestStatus] = mapped_column(
        str_enum(PaymentRequestStatus, "status"), server_default=PaymentRequestStatus.ACTIVE.value
    )
    tag_reserved: Mapped[bool] = mapped_column(server_default=sa.false())
    tag_release_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.CheckConstraint("expected_amount > 0", name="expected_positive"),
        sa.Index(
            "uq_payment_requests_one_active",
            "deal_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
        # The unique-amount tag must be unique among reserved requests on the same shared address.
        sa.Index(
            "uq_payment_requests_tag",
            "network",
            "asset",
            "deposit_address",
            "expected_amount",
            unique=True,
            postgresql_where=sa.text("tag_reserved"),
        ),
        sa.Index(
            "uq_payment_requests_external",
            "provider",
            "external_id",
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_payment_requests_sol_reference",
            "sol_reference",
            unique=True,
            postgresql_where=sa.text("sol_reference IS NOT NULL"),
        ),
        sa.Index("ix_payment_requests_memo", "network", "memo", postgresql_where=sa.text("memo IS NOT NULL")),
        sa.Index(
            "ix_payment_requests_tag_release", "tag_release_at", postgresql_where=sa.text("tag_reserved")
        ),
    )


class IncomingTransfer(Base):
    __tablename__ = "incoming_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[Provider] = mapped_column(str_enum(Provider, "provider"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    asset: Mapped[Asset] = mapped_column(str_enum(Asset, "asset"))
    token_contract: Mapped[str | None]
    tx_hash: Mapped[str]  # CryptoBot: invoice_id
    event_index: Mapped[int] = mapped_column(sa.Integer)  # log index / instruction index / 0
    from_address: Mapped[str | None]
    to_address: Mapped[str]
    amount: Mapped[int] = mapped_column(MinorAmount)
    memo: Mapped[str | None]
    sol_references: Mapped[list[str] | None] = mapped_column(sa.ARRAY(sa.Text))
    block_number: Mapped[int | None]
    # Place in the network's scan order (EVM block, TRON block timestamp in ms, TON lt, Solana slot):
    # a transfer seen at a position the final scan has passed without finding it was dropped.
    position: Mapped[int | None]
    block_hash: Mapped[str | None]
    block_time: Mapped[datetime | None]
    status: Mapped[TransferStatus] = mapped_column(str_enum(TransferStatus, "status"))
    match_status: Mapped[MatchStatus] = mapped_column(
        str_enum(MatchStatus, "match_status"), server_default=MatchStatus.PENDING.value
    )
    match_method: Mapped[MatchMethod | None] = mapped_column(str_enum(MatchMethod, "match_method"))
    payment_request_id: Mapped[int | None] = mapped_column(sa.ForeignKey("payment_requests.id"))
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    verified_by: Mapped[list[str]] = mapped_column(sa.ARRAY(sa.Text), server_default="{}")
    raw: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    first_seen_at: Mapped[datetime] = created_at_column()
    finalized_at: Mapped[datetime | None]

    __table_args__ = (
        # One on-chain event can fund at most one deal.
        sa.UniqueConstraint("network", "tx_hash", "event_index", name="uq_incoming_transfers_event"),
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.Index(
            "ix_incoming_transfers_unidentified",
            "first_seen_at",
            postgresql_where=sa.text("match_status = 'unidentified'"),
        ),
        sa.Index(
            "ix_incoming_transfers_seen", "network", "status", postgresql_where=sa.text("status = 'seen'")
        ),
    )


class TxClaim(Base):
    __tablename__ = "tx_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    tx_hash: Mapped[str]
    incoming_transfer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("incoming_transfers.id"))
    status: Mapped[TxClaimStatus] = mapped_column(str_enum(TxClaimStatus, "status"))
    reviewed_by: Mapped[int | None]
    result: Mapped[str | None]  # what the lookup found, for the buyer and the admins
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.Index("ix_tx_claims_submitted", "created_at", postgresql_where=sa.text("status = 'submitted'")),
    )


class ChainCursor(Base):
    __tablename__ = "chain_cursors"

    network: Mapped[Network] = mapped_column(str_enum(Network, "network"), primary_key=True)
    stream: Mapped[str] = mapped_column(primary_key=True)
    # Opaque cursors: block number / TON lt / Solana signature / TRON timestamp+id.
    cursor_seen: Mapped[str | None]
    cursor_final: Mapped[str | None]
    head_latest: Mapped[int | None]
    head_final: Mapped[int | None]
    last_error: Mapped[str | None]
    last_error_at: Mapped[datetime | None]
    active_endpoint: Mapped[str | None]
    updated_at: Mapped[datetime] = updated_at_column()
