from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum
from app.domain.enums import OutboxStatus


class NotificationOutbox(Base):
    """Telegram notifications written in the same DB transaction as the state change (outbox)."""

    __tablename__ = "notifications_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    deal_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deals.id"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    status: Mapped[OutboxStatus] = mapped_column(
        str_enum(OutboxStatus, "status"), server_default=OutboxStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = created_at_column()
    tg_message_id: Mapped[int | None]
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.Index(
            "ix_notifications_outbox_pending",
            "next_attempt_at",
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(sa.String(64))
    target_type: Mapped[str | None] = mapped_column(sa.String(32))
    target_id: Mapped[int | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_column()


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet: Mapped[str]
    asset: Mapped[str] = mapped_column(sa.String(32))
    chain_balance: Mapped[int | None] = mapped_column(MinorAmount)
    ledger_balance: Mapped[int | None] = mapped_column(MinorAmount)
    in_flight: Mapped[int | None] = mapped_column(MinorAmount)
    diff: Mapped[int | None] = mapped_column(MinorAmount)
    ok: Mapped[bool]
    created_at: Mapped[datetime] = created_at_column()
