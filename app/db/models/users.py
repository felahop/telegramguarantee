from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, MinorAmount, created_at_column, str_enum
from app.domain.enums import Network, UserRole


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)  # Telegram user_id
    # Not unique: our copy can be stale; on update the username is cleared from other rows.
    username: Mapped[str | None]
    username_changed_at: Mapped[datetime | None]
    first_name: Mapped[str] = mapped_column(server_default="")
    language_code: Mapped[str | None]
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole, "role"), server_default=UserRole.USER.value)
    is_banned: Mapped[bool] = mapped_column(server_default=sa.false())
    ban_reason: Mapped[str | None]
    bot_blocked: Mapped[bool] = mapped_column(server_default=sa.false())
    deals_completed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    deals_disputed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    disputes_lost: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    volume_usdt: Mapped[int] = mapped_column(MinorAmount, server_default="0")  # micro-USDT (6 dp)
    limit_tier: Mapped[int] = mapped_column(sa.SmallInteger, server_default="0")
    totp_secret_enc: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    totp_confirmed_at: Mapped[datetime | None]  # NULL while enrollment is pending
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = created_at_column()
    # Set on /start: only users who started the bot can receive messages from it.
    started_at: Mapped[datetime | None]

    __table_args__ = (sa.Index("ix_users_username_lower", sa.text("lower(username)")),)


class UsernameHistory(Base):
    __tablename__ = "username_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    username: Mapped[str | None]
    seen_at: Mapped[datetime] = created_at_column()


class SavedAddress(Base):
    __tablename__ = "saved_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))
    network: Mapped[Network] = mapped_column(str_enum(Network, "network"))
    address: Mapped[str]
    label: Mapped[str | None]
    created_at: Mapped[datetime] = created_at_column()
    last_used_at: Mapped[datetime | None]

    __table_args__ = (sa.UniqueConstraint("user_id", "network", "address"),)


class Blacklist(Base):
    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(index=True)
    network: Mapped[Network | None] = mapped_column(str_enum(Network, "network"))
    address: Mapped[str | None] = mapped_column(index=True)
    reason: Mapped[str]
    created_by: Mapped[int]
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (sa.CheckConstraint("user_id IS NOT NULL OR address IS NOT NULL", name="target"),)
