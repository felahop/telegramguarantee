"""User registry: upsert on every update, username history, admin bootstrap."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Blacklist, User, UsernameHistory
from app.domain.enums import UserRole
from app.domain.errors import Forbidden, NotFound


async def touch_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    first_name: str,
    language_code: str | None,
    now: datetime,
) -> User:
    """Create or refresh a user. A username change is recorded (shown as a red flag on cards) and
    the username is cleared from any stale row that still carries it."""
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        user = User(id=user_id, username=username, first_name=first_name, language_code=language_code)
        session.add(user)
        await session.flush()
        session.add(UsernameHistory(user_id=user_id, username=username))
    else:
        if (user.username or None) != (username or None):
            session.add(UsernameHistory(user_id=user_id, username=username))
            user.username = username
            user.username_changed_at = now
        user.first_name = first_name
        user.language_code = language_code
        user.last_seen_at = now
    if username:
        await session.execute(
            sa.update(User)
            .where(sa.func.lower(User.username) == username.lower(), User.id != user_id)
            .values(username=None)
        )
    await session.flush()
    return user


async def find_by_username(session: AsyncSession, username: str) -> User | None:
    name = username.strip().lstrip("@").lower()
    if not name:
        return None
    result = await session.execute(sa.select(User).where(sa.func.lower(User.username) == name).limit(1))
    return result.scalar_one_or_none()


async def get_active_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("user not found", user_id=user_id)
    if user.is_banned or await is_blacklisted(session, user_id=user_id):
        raise Forbidden("user is banned", user_id=user_id)
    return user


async def is_blacklisted(
    session: AsyncSession, *, user_id: int | None = None, address: str | None = None
) -> bool:
    conditions = []
    if user_id is not None:
        conditions.append(Blacklist.user_id == user_id)
    if address is not None:
        conditions.append(sa.func.lower(Blacklist.address) == address.lower())
    if not conditions:
        return False
    result = await session.execute(sa.select(sa.literal(True)).where(sa.or_(*conditions)).limit(1))
    return result.scalar_one_or_none() is not None


async def ensure_admins(session: AsyncSession, admin_ids: Iterable[int]) -> None:
    ids = list(admin_ids)
    if ids:
        await session.execute(sa.update(User).where(User.id.in_(ids)).values(role=UserRole.ADMIN))
