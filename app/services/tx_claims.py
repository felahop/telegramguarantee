"""The buyer's fallback: "I paid, here is the transaction hash" (docs §7.5).

A claim never binds money by itself. The worker looks the transaction up, and only USDT transfers
to our own address in a final transaction are credited — through the watcher's normal path
(second source, then the matching rules: comment, reference, exact tagged amount). If the
transfer does not match this deal by those rules, it lands in the admins' queue with the claim as
evidence. A hash already credited to another deal is refused and reported (someone may be trying
to claim another buyer's payment).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chains.base import ChainAdapter, ChainUnavailable
from app.chains.watcher import ChainWatcher
from app.db.models import Deal, IncomingTransfer, TxClaim
from app.domain.enums import DealStatus, MatchStatus, Network, Provider, TxClaimStatus
from app.domain.errors import Forbidden, InvalidTransition, LimitExceeded, ValidationFailed
from app.logging import get_logger
from app.services import notifications
from app.services.notifications import Notice

log = get_logger(__name__)

CLAIMS_PER_HOUR = 5
CLAIMABLE = frozenset({DealStatus.AWAITING_PAYMENT, DealStatus.EXPIRED, DealStatus.CANCELLED})
_HASH = {
    Network.BSC: re.compile(r"^0x[0-9a-f]{64}$"),
    Network.ETH: re.compile(r"^0x[0-9a-f]{64}$"),
    Network.TRON: re.compile(r"^[0-9a-f]{64}$"),
    Network.SOL: re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$"),
}
PENDING_FOR = timedelta(hours=2)  # a claimed transaction that is not final yet is re-checked this long


def normalize_hash(network: Network, text: str) -> str:
    value = text.strip()
    if network in (Network.BSC, Network.ETH):
        value = value.lower()
        value = value if value.startswith("0x") else "0x" + value
    elif network is Network.TRON:
        value = value.lower().removeprefix("0x")
    pattern = _HASH.get(network)
    if pattern is None or not pattern.fullmatch(value):
        raise ValidationFailed(
            "this does not look like a transaction hash of the deal's network", field="tx_hash"
        )
    return value


async def submit_claim(
    session: AsyncSession, *, user_id: int, deal_id: int, tx_hash: str, now: datetime
) -> TxClaim:
    deal = await session.get(Deal, deal_id)
    if deal is None or deal.buyer_id != user_id:
        raise Forbidden("only the buyer of the deal can send its transaction")
    if deal.provider is not Provider.ONCHAIN:
        raise InvalidTransition("this deal is paid through CryptoBot")
    if deal.status not in CLAIMABLE:
        raise InvalidTransition("the deal is not waiting for a payment", status=deal.status)
    value = normalize_hash(deal.network, tx_hash)
    existing = (
        await session.execute(sa.select(TxClaim).where(TxClaim.deal_id == deal.id, TxClaim.tx_hash == value))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    recent = await session.scalar(
        sa.select(sa.func.count())
        .select_from(TxClaim)
        .where(TxClaim.user_id == user_id, TxClaim.created_at >= sa.func.now() - timedelta(hours=1))
    )
    if int(recent or 0) >= CLAIMS_PER_HOUR:
        raise LimitExceeded("too many transaction checks, try again in an hour")
    claim = TxClaim(
        deal_id=deal.id, user_id=user_id, network=deal.network, tx_hash=value, status=TxClaimStatus.SUBMITTED
    )
    session.add(claim)
    await session.flush()
    log.info("tx_claim_submitted", deal_id=deal.id, claim_id=claim.id, network=deal.network.value)
    return claim


async def process_claims(
    sessionmaker: async_sessionmaker[AsyncSession],
    adapters: Mapping[Network, ChainAdapter],
    watchers: Mapping[Network, ChainWatcher],
    *,
    clock: Callable[[], datetime],
    limit: int = 20,
) -> int:
    """Look submitted claims up (worker side: only the worker talks to the chains)."""
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                sa.select(TxClaim, sa.func.now() - TxClaim.created_at)  # age by the database clock
                .where(TxClaim.status == TxClaimStatus.SUBMITTED)
                .order_by(TxClaim.created_at)
                .limit(limit)
            )
        ).all()
    done = 0
    for claim, raw_age in rows:
        age = raw_age if isinstance(raw_age, timedelta) else timedelta(0)
        adapter, watcher = adapters.get(claim.network), watchers.get(claim.network)
        if adapter is None or watcher is None:
            await _finish(sessionmaker, claim, TxClaimStatus.REJECTED, "network_not_watched", clock())
            done += 1
            continue
        try:
            found = await adapter.lookup_tx(claim.tx_hash)
            head = await adapter.head()
        except ChainUnavailable as exc:
            log.warning("tx_claim_lookup_unavailable", claim_id=claim.id, error=str(exc)[:120])
            continue
        final = [t for t in found if t.position <= head.final]
        if not final:
            if found and age < PENDING_FOR:
                continue  # in a block, not final yet: look again later
            await _finish(sessionmaker, claim, TxClaimStatus.REJECTED, "no_usdt_to_us", clock())
            done += 1
            continue
        try:
            transfer_ids = await watcher.ingest(final)
        except ChainUnavailable:
            continue  # the second source cannot tell yet
        await _resolve(sessionmaker, claim, transfer_ids, clock())
        done += 1
    return done


async def _resolve(
    sessionmaker: async_sessionmaker[AsyncSession], claim: TxClaim, transfer_ids: list[int], now: datetime
) -> None:
    async with sessionmaker() as session:
        transfers = list(
            (
                await session.execute(
                    sa.select(IncomingTransfer).where(IncomingTransfer.id.in_(transfer_ids))
                )
            ).scalars()
        )
    ours = [t for t in transfers if t.deal_id == claim.deal_id]
    others = [t for t in transfers if t.deal_id is not None and t.deal_id != claim.deal_id]
    if ours:
        await _finish(sessionmaker, claim, TxClaimStatus.AUTO_MATCHED, "credited", now, ours[0].id)
    elif others:
        await _finish(
            sessionmaker,
            claim,
            TxClaimStatus.REJECTED,
            "belongs_to_another_deal",
            now,
            others[0].id,
            alert=True,
        )
    else:
        unidentified = [t for t in transfers if t.match_status is MatchStatus.UNIDENTIFIED]
        target = unidentified[0].id if unidentified else (transfers[0].id if transfers else None)
        await _finish(
            sessionmaker, claim, TxClaimStatus.PENDING_REVIEW, "needs_admin", now, target, alert=True
        )


async def _finish(  # noqa: PLR0917 - internal
    sessionmaker: async_sessionmaker[AsyncSession],
    claim: TxClaim,
    status: TxClaimStatus,
    result: str,
    now: datetime,
    transfer_id: int | None = None,
    *,
    alert: bool = False,
) -> None:
    async with sessionmaker() as session, session.begin():
        row = await session.get(TxClaim, claim.id, with_for_update=True)
        if row is None or row.status is not TxClaimStatus.SUBMITTED:
            return
        row.status = status
        row.result = result
        row.incoming_transfer_id = transfer_id
        await notifications.enqueue(
            session,
            user_id=row.user_id,
            kind=Notice.TX_CLAIM_RESULT,
            deal_id=row.deal_id,
            dedupe_key=f"tx-claim:{row.id}",
            now=now,
            payload={"result": result, "tx_hash": row.tx_hash},
        )
        if alert:
            for admin_id in await notifications.admin_ids(session):
                await notifications.enqueue(
                    session,
                    user_id=admin_id,
                    kind=Notice.ADMIN_PAYMENT_ANOMALY,
                    deal_id=row.deal_id,
                    dedupe_key=f"tx-claim-admin:{row.id}:{admin_id}",
                    now=now,
                    payload={"tx": f"{row.network.value}:{row.tx_hash}", "problems": f"buyer_claim:{result}"},
                )
    log.info("tx_claim_processed", claim_id=claim.id, status=status.value, result=result)
