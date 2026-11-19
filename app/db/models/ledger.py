from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum
from app.domain.enums import AccountKind

# Sign convention: entries are signed, debit = positive, credit = negative.
# Asset/expense accounts therefore hold a non-negative balance, liability/revenue a non-positive one.
BALANCE_SIGN_CHECK = (
    "allow_negative "
    "OR (kind IN ('asset', 'expense') AND balance >= 0) "
    "OR (kind IN ('liability', 'revenue', 'equity') AND balance <= 0)"
)


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(48))
    kind: Mapped[AccountKind] = mapped_column(str_enum(AccountKind, "kind"))
    asset: Mapped[str] = mapped_column(sa.String(32))  # e.g. "USDT@tron", "TRX@tron"
    wallet: Mapped[str | None]
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"))
    user_id: Mapped[int | None]
    # Cache maintained by a trigger on ledger_entries; the entries are the source of truth.
    balance: Mapped[int] = mapped_column(MinorAmount, server_default="0")
    allow_negative: Mapped[bool] = mapped_column(server_default=sa.false())
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.UniqueConstraint(
            "code",
            "asset",
            "wallet",
            "deal_id",
            "user_id",
            name="uq_ledger_accounts_identity",
            postgresql_nulls_not_distinct=True,
        ),
        # Target of the composite FK from entries: an entry's asset must equal its account's asset.
        sa.UniqueConstraint("id", "asset", name="uq_ledger_accounts_id_asset"),
        sa.CheckConstraint(BALANCE_SIGN_CHECK, name="balance_sign"),
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), unique=True)
    kind: Mapped[str] = mapped_column(sa.String(48))
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    ref_type: Mapped[str | None] = mapped_column(sa.String(32))
    ref_id: Mapped[int | None]
    fingerprint: Mapped[str] = mapped_column(sa.String(64))  # sha256 of the postings, for replay checks
    reverses_id: Mapped[int | None] = mapped_column(sa.ForeignKey("ledger_transactions.id"), unique=True)
    meta: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_column()


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(sa.ForeignKey("ledger_transactions.id"), index=True)
    account_id: Mapped[int] = mapped_column(index=True)
    asset: Mapped[str] = mapped_column(sa.String(32))
    amount: Mapped[int] = mapped_column(MinorAmount)

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["account_id", "asset"],
            ["ledger_accounts.id", "ledger_accounts.asset"],
            name="fk_ledger_entries_account_asset",
        ),
        sa.CheckConstraint("amount <> 0", name="non_zero"),
    )
