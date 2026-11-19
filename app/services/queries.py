"""Read models and small user-owned records used by the bot UI (no money effects)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, DealTemplate, SavedAddress, User
from app.domain.enums import TERMINAL_STATUSES, FeePayer, Network, PartyRole
from app.domain.errors import LimitExceeded, NotFound
from app.domain.money import from_minor

MAX_TEMPLATES = 10
MAX_SAVED_ADDRESSES = 5


def _involving(user_id: int) -> sa.ColumnElement[bool]:
    return sa.or_(Deal.buyer_id == user_id, Deal.seller_id == user_id, Deal.creator_id == user_id)


async def load_users(session: AsyncSession, ids: Sequence[int | None]) -> dict[int, User]:
    wanted = [i for i in set(ids) if i is not None]
    if not wanted:
        return {}
    rows = await session.execute(sa.select(User).where(User.id.in_(wanted)))
    return {u.id: u for u in rows.scalars()}


async def user_deals(
    session: AsyncSession, user_id: int, *, offset: int = 0, limit: int = 10
) -> tuple[list[Deal], bool]:
    """Active deals first, then the most recent closed ones. Returns (page, has_more)."""
    active_first = sa.case((Deal.status.in_(list(TERMINAL_STATUSES)), 1), else_=0)
    rows = await session.execute(
        sa.select(Deal)
        .where(_involving(user_id))
        .order_by(active_first, Deal.created_at.desc(), Deal.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    deals = list(rows.scalars())
    return deals[:limit], len(deals) > limit


async def recent_counterparties(
    session: AsyncSession, user_id: int, *, limit: int = 3
) -> list[tuple[int, str]]:
    other = sa.case((Deal.buyer_id == user_id, Deal.seller_id), else_=Deal.buyer_id)
    sub = (
        sa.select(other.label("uid"), sa.func.max(Deal.created_at).label("last"))
        .where(sa.or_(Deal.buyer_id == user_id, Deal.seller_id == user_id), other.is_not(None))
        .group_by(other)
        .subquery()
    )
    rows = await session.execute(
        sa.select(User).join(sub, sub.c.uid == User.id).order_by(sub.c.last.desc()).limit(limit)
    )
    return [(u.id, f"@{u.username}" if u.username else u.first_name or f"id{u.id}") for u in rows.scalars()]


async def recent_amounts(session: AsyncSession, user_id: int, *, limit: int = 3) -> list[Decimal]:
    rows = await session.execute(
        sa.select(Deal.amount, Deal.decimals)
        .where(Deal.creator_id == user_id)
        .order_by(Deal.id.desc())
        .limit(20)
    )
    result: list[Decimal] = []
    for amount, decimals in rows.all():
        value = from_minor(amount, decimals).normalize()
        if value not in result:
            result.append(value)
        if len(result) == limit:
            break
    return result


@dataclass(frozen=True)
class Defaults:
    network: Network | None
    fee_payer: FeePayer
    hours: int


async def last_defaults(session: AsyncSession, user_id: int) -> Defaults:
    deal = (
        await session.execute(
            sa.select(Deal).where(Deal.creator_id == user_id).order_by(Deal.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if deal is None:
        return Defaults(network=None, fee_payer=FeePayer.BUYER, hours=24)
    return Defaults(network=deal.network, fee_payer=deal.fee_payer, hours=deal.execution_hours)


# ── saved payout addresses ───────────────────────────────────────────────────


async def saved_addresses(session: AsyncSession, user_id: int, network: Network) -> list[SavedAddress]:
    rows = await session.execute(
        sa.select(SavedAddress)
        .where(SavedAddress.user_id == user_id, SavedAddress.network == network)
        .order_by(sa.func.coalesce(SavedAddress.last_used_at, SavedAddress.created_at).desc())
        .limit(MAX_SAVED_ADDRESSES)
    )
    return list(rows.scalars())


async def get_saved_address(session: AsyncSession, user_id: int, address_id: int) -> SavedAddress:
    saved = await session.get(SavedAddress, address_id)
    if saved is None or saved.user_id != user_id:
        raise NotFound("address not found")
    return saved


async def remember_address(
    session: AsyncSession, user_id: int, network: Network, address: str, *, now: datetime
) -> None:
    existing = (
        await session.execute(
            sa.select(SavedAddress).where(
                SavedAddress.user_id == user_id,
                SavedAddress.network == network,
                SavedAddress.address == address,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_used_at = now
    else:
        session.add(SavedAddress(user_id=user_id, network=network, address=address, last_used_at=now))
    await session.flush()


# ── templates ────────────────────────────────────────────────────────────────


async def list_templates(session: AsyncSession, user_id: int) -> list[DealTemplate]:
    rows = await session.execute(
        sa.select(DealTemplate).where(DealTemplate.user_id == user_id).order_by(DealTemplate.id.desc())
    )
    return list(rows.scalars())


async def get_template(session: AsyncSession, user_id: int, template_id: int) -> DealTemplate:
    template = await session.get(DealTemplate, template_id)
    if template is None or template.user_id != user_id:
        raise NotFound("template not found")
    return template


async def save_template(session: AsyncSession, user_id: int, deal: Deal) -> DealTemplate:
    count = await session.scalar(
        sa.select(sa.func.count()).select_from(DealTemplate).where(DealTemplate.user_id == user_id)
    )
    if (count or 0) >= MAX_TEMPLATES:
        raise LimitExceeded("too many templates", kind="templates", limit=MAX_TEMPLATES)
    role = deal.party_role(user_id) or PartyRole.BUYER
    amount = f"{from_minor(deal.amount, deal.decimals).normalize():f}"
    title = deal.terms.splitlines()[0][:30]
    template = DealTemplate(
        user_id=user_id,
        name=f"{amount} USDT · {title}",
        role=role,
        network=deal.network,
        amount=deal.amount,
        terms=deal.terms,
        execution_hours=deal.execution_hours,
        fee_payer=deal.fee_payer,
    )
    session.add(template)
    await session.flush()
    return template


async def delete_template(session: AsyncSession, user_id: int, template_id: int) -> None:
    template = await get_template(session, user_id, template_id)
    await session.delete(template)
    await session.flush()
