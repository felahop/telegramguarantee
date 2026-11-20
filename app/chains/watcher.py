"""ChainWatcher: one loop per network with two cursors (docs §8.1).

* **seen-scan** follows the latest head, only for UX: "payment found, waiting for confirmations",
  and to keep the payment window open while a transfer confirms. It never moves money.
* **final-scan** reads only up to the final head, where reorganizations do not happen. Every
  transfer it returns is checked with an independent source and then credited in the same DB
  transaction that advances the cursor; replaying a range is harmless (unique event index).
* a transfer the seen-scan reported that the final-scan has passed without finding is
  **dropped** (reorg or a failed transaction) and the buyer is told.

Network calls happen before the DB transaction; the transaction re-checks that the cursor did not
move meanwhile (another replica), so several workers can run the same watcher safely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chains.base import ChainAdapter, ChainHead, ObservedTransfer
from app.config import ChainsConfig, Settings
from app.db.models import ChainCursor, IncomingTransfer
from app.domain.enums import Asset, MatchStatus, Provider, TransferStatus
from app.logging import get_logger
from app.services import notifications
from app.services.deals import PaymentGateway
from app.services.notifications import Notice
from app.services.payments import PaymentService

log = get_logger(__name__)
STREAM = "deposits"


@dataclass(frozen=True)
class TickResult:
    seen: int = 0
    final: int = 0
    dropped: int = 0
    behind: bool = False  # more to scan right away
    skipped: bool = False  # another replica advanced the cursor meanwhile


@dataclass(frozen=True)
class _Cursor:
    seen: int
    final: int


class ChainWatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        chains: ChainsConfig,
        adapter: ChainAdapter,
        *,
        clock: Callable[[], datetime],
        gateway: PaymentGateway | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._settings = settings
        self._chains = chains
        self._adapter = adapter
        self._clock = clock
        self._gateway = gateway
        self.network = adapter.network

    async def tick(self) -> TickResult:
        head = await self._adapter.head()
        cursor = await self._cursor(head)
        span = self._adapter.max_span
        final_upto = min(head.final, cursor.final + span)
        seen_from = max(cursor.seen, final_upto)  # what the final scan covers needs no seen pass
        seen_upto = min(head.latest, seen_from + span)
        final = (
            await self._adapter.scan(cursor.final, final_upto, final=True)
            if final_upto > cursor.final
            else []
        )
        final_keys = {t.key for t in final}
        seen = (
            [
                t
                for t in await self._adapter.scan(seen_from, seen_upto, final=False)
                if t.key not in final_keys
            ]
            if seen_upto > seen_from
            else []
        )
        verdicts = {t.key: await self._adapter.verify(t) for t in final}

        async with self._sm() as session, session.begin():
            row = await session.get(ChainCursor, (self.network, STREAM), with_for_update=True)
            if row is None or _int(row.cursor_final) != cursor.final or _int(row.cursor_seen) != cursor.seen:
                return TickResult(skipped=True)
            payments = PaymentService(
                session, self._settings, self._chains, gateway=self._gateway, clock=self._clock
            )
            new_seen = 0
            for t in seen:
                new_seen += await self._upsert_seen(session, payments, t)
            for t in final:
                await self._finalize(session, payments, t, verified=verdicts[t.key])
            dropped = await self._drop_unconfirmed(session, payments, upto=final_upto, keep=final_keys)
            row.cursor_final = str(final_upto)
            row.cursor_seen = str(max(cursor.seen, seen_upto, final_upto))
            row.head_latest = head.latest
            row.head_final = head.final
            row.active_endpoint = self._adapter.active_endpoint
            row.last_error = None
        if final or seen or dropped:
            log.info(
                "chain_tick",
                network=self.network.value,
                final=len(final),
                seen=new_seen,
                dropped=dropped,
                cursor_final=final_upto,
                head_final=head.final,
            )
        return TickResult(
            seen=new_seen,
            final=len(final),
            dropped=dropped,
            behind=final_upto < head.final or seen_upto < head.latest,
        )

    async def ingest(self, transfers: list[ObservedTransfer]) -> list[int]:
        """Credit transfers found outside the scan (a buyer's tx-hash claim), exactly as the final scan
        would: only final ones (the caller checks), each confirmed by the second source, then the
        usual matching rules. Already recorded transfers are left alone. Returns their row ids."""
        verdicts = {t.key: await self._adapter.verify(t) for t in transfers}
        async with self._sm() as session, session.begin():
            payments = PaymentService(
                session, self._settings, self._chains, gateway=self._gateway, clock=self._clock
            )
            for t in transfers:
                await self._finalize(session, payments, t, verified=verdicts[t.key])
            ids = await session.execute(
                sa.select(IncomingTransfer.id).where(
                    IncomingTransfer.network == self.network,
                    sa.tuple_(IncomingTransfer.tx_hash, IncomingTransfer.event_index).in_(
                        [t.key for t in transfers]
                    ),
                )
            )
            return list(ids.scalars())

    async def record_error(self, exc: BaseException) -> None:
        async with self._sm() as session, session.begin():
            await self._ensure_row(session, None)
            await session.execute(
                sa.update(ChainCursor)
                .where(ChainCursor.network == self.network, ChainCursor.stream == STREAM)
                .values(last_error=repr(exc)[:500], last_error_at=self._clock())
            )

    # ── internals ──────────────────────────────────────────────────────────
    async def _cursor(self, head: ChainHead) -> _Cursor:
        """The first run starts one span behind the final head: no historic rescan of the chain."""
        start = max(head.final - self._adapter.max_span, 0)
        async with self._sm() as session, session.begin():
            row = await self._ensure_row(session, start)
        return _Cursor(seen=_int(row.cursor_seen), final=_int(row.cursor_final))

    async def _ensure_row(self, session: AsyncSession, start: int | None) -> ChainCursor:
        values = {"network": self.network, "stream": STREAM}
        if start is not None:
            values |= {"cursor_seen": str(start), "cursor_final": str(start)}
        await session.execute(pg_insert(ChainCursor).values(**values).on_conflict_do_nothing())
        row = await session.get(ChainCursor, (self.network, STREAM), populate_existing=True)
        if row is None:  # pragma: no cover - inserted just above
            raise RuntimeError("chain cursor row is missing")
        if row.cursor_final is None and start is not None:  # created by record_error before any head
            row.cursor_seen = row.cursor_final = str(start)
        return row

    async def _upsert_seen(self, session: AsyncSession, payments: PaymentService, t: ObservedTransfer) -> int:
        inserted = await session.execute(
            pg_insert(IncomingTransfer)
            .values(**self._values(t), status=TransferStatus.SEEN, match_status=MatchStatus.PENDING)
            .on_conflict_do_nothing(constraint="uq_incoming_transfers_event")
            .returning(IncomingTransfer.id)
        )
        transfer_id = inserted.scalar_one_or_none()
        if transfer_id is None:
            return 0
        transfer = await session.get(IncomingTransfer, transfer_id)
        if transfer is not None:
            await payments.note_seen(transfer)
        return 1

    async def _finalize(
        self, session: AsyncSession, payments: PaymentService, t: ObservedTransfer, *, verified: bool
    ) -> None:
        transfer = (
            await session.execute(
                sa.select(IncomingTransfer)
                .where(
                    IncomingTransfer.network == t.network,
                    IncomingTransfer.tx_hash == t.tx_hash,
                    IncomingTransfer.event_index == t.event_index,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if transfer is not None and transfer.status in (TransferStatus.FINAL, TransferStatus.NEEDS_REVIEW):
            return  # this range was processed before
        values = self._values(t)
        if transfer is None:
            transfer = IncomingTransfer(**values, match_status=MatchStatus.PENDING)
            session.add(transfer)
        else:  # seen (or dropped and back after a reorg): the final data wins
            for name, value in values.items():
                if name != "first_seen_at":
                    setattr(transfer, name, value)
        transfer.status = TransferStatus.FINAL if verified else TransferStatus.NEEDS_REVIEW
        transfer.finalized_at = self._clock()
        transfer.verified_by = [self._adapter.active_endpoint or "primary", "verify"] if verified else []
        await session.flush()
        if verified:
            await payments.apply_onchain_transfer(transfer)
            return
        log.error("onchain_transfer_verification_failed", network=t.network.value, tx_hash=t.tx_hash)
        for admin_id in await notifications.admin_ids(session):
            await notifications.enqueue(
                session,
                user_id=admin_id,
                kind=Notice.ADMIN_PAYMENT_ANOMALY,
                dedupe_key=f"unverified:{t.network.value}:{t.tx_hash}:{t.event_index}:{admin_id}"[:128],
                now=self._clock(),
                payload={
                    "tx": f"{t.network.value}:{t.tx_hash}",
                    "amount": t.amount,
                    "problems": "second_source_disagrees",
                },
            )

    async def _drop_unconfirmed(
        self, session: AsyncSession, payments: PaymentService, *, upto: int, keep: set[tuple[str, int]]
    ) -> int:
        rows = await session.execute(
            sa.select(IncomingTransfer)
            .where(
                IncomingTransfer.network == self.network,
                IncomingTransfer.status == TransferStatus.SEEN,
                IncomingTransfer.position <= upto,
            )
            .with_for_update()
        )
        dropped = 0
        for transfer in rows.scalars():
            if (transfer.tx_hash, transfer.event_index) not in keep:
                await payments.note_dropped(transfer)
                dropped += 1
        return dropped

    def _values(self, t: ObservedTransfer) -> dict[str, object]:
        return {
            "provider": Provider.ONCHAIN,
            "network": t.network,
            "asset": Asset.USDT,
            "token_contract": t.token_contract,
            "tx_hash": t.tx_hash,
            "event_index": t.event_index,
            "from_address": t.from_address,
            "to_address": t.to_address,
            "amount": t.amount,
            "memo": t.memo,
            "sol_references": list(t.references) or None,
            "block_number": t.block_number,
            "position": t.position,
            "block_hash": t.block_hash,
            "block_time": t.block_time,
            "raw": t.raw,
            "first_seen_at": self._clock(),
        }


def _int(value: str | None) -> int:
    return int(value) if value else 0


async def run_watcher(watcher: ChainWatcher, poll_seconds: float, stop: asyncio.Event) -> None:
    log.info("chain_watcher_started", network=watcher.network.value)
    while not stop.is_set():
        behind = False
        try:
            behind = (await watcher.tick()).behind
        except Exception as exc:  # an RPC outage must not kill the loop; the error is visible in the admin
            log.warning("chain_watcher_error", network=watcher.network.value, error=repr(exc))
            try:
                await watcher.record_error(exc)
            except Exception as db_exc:
                log.error("chain_watcher_record_failed", error=repr(db_exc))
        if behind:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
