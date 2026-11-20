"""CryptoBot background jobs: invoice polling (missed webhooks) and payouts via ``transfer``.

Payout idempotency: ``spend_id`` = the payout's idempotency key, which Crypto Pay accepts only once
per app. Any uncertain outcome (timeout, crash after the call, "spend_id already used") is resolved
by looking the transfer up by ``spend_id`` — never by sending a new transfer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.context import BotContext
from app.db.models import Deal, PaymentRequest, Payout, PayoutPause
from app.domain.enums import (
    BUYER_BOUND_PAYOUTS,
    Asset,
    Network,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
)
from app.domain.money import asset_code, from_minor
from app.logging import get_logger
from app.providers.cryptobot.client import CryptoPayClient, CryptoPayError, Transfer
from app.services import ledger as postings
from app.services import notifications
from app.services.deals import DealService
from app.services.ledger import Ledger
from app.services.notifications import Notice
from app.services.payments import CRYPTOBOT_WALLET, PAYOUT_OUTCOME_UNKNOWN, PaymentService

log = get_logger(__name__)

POLL_SECONDS = 30.0
PAYOUT_IDLE_SECONDS = 5.0
RECIPIENT_RETRY = timedelta(minutes=30)
FUNDS_RETRY = timedelta(minutes=10)
MAX_PAYOUT_ATTEMPTS = 20


async def poll_invoices(ctx: BotContext, client: CryptoPayClient) -> int:
    """Apply paid invoices that no webhook told us about. Returns how many were applied."""
    async with ctx.sessionmaker() as session:
        rows = await session.execute(
            sa.select(PaymentRequest.external_id).where(
                PaymentRequest.provider == Provider.CRYPTOBOT,
                PaymentRequest.status == PaymentRequestStatus.ACTIVE,
                PaymentRequest.external_id.is_not(None),
            )
        )
        invoice_ids = [int(x) for x in rows.scalars() if x]
    if not invoice_ids:
        return 0
    applied = 0
    for invoice in await client.get_invoices(invoice_ids, status="paid"):
        async with ctx.sessionmaker() as session, session.begin():
            result = await PaymentService(
                session, ctx.settings, ctx.chains, gateway=ctx.gateway, clock=ctx.clock
            ).apply_cryptobot_invoice(invoice)
        log.info("cryptobot_poll_applied", invoice_id=invoice.invoice_id, result=result.value)
        applied += 1
    return applied


class CryptoBotPayouts:
    def __init__(self, ctx: BotContext, client: CryptoPayClient) -> None:
        self._ctx = ctx
        self._client = client

    async def run_once(self) -> bool:
        """Execute one due CryptoBot payout. Returns False when nothing is due (or payouts are paused)."""
        now = self._ctx.clock()
        async with self._ctx.sessionmaker() as session, session.begin():
            if await session.get(PayoutPause, CRYPTOBOT_WALLET) is not None:
                return False
            payout = (
                await session.execute(
                    sa.select(Payout)
                    .where(
                        Payout.provider == Provider.CRYPTOBOT,
                        Payout.status.in_([PayoutStatus.QUEUED, PayoutStatus.FAILED_RETRYABLE]),
                        Payout.next_attempt_at <= now,
                    )
                    .order_by(Payout.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if payout is None:
                return False
            deal = await session.get(Deal, payout.deal_id) if payout.deal_id else None
            payout.attempts += 1
            transfer = await self._execute(payout, deal, now)
            if transfer is not None:
                await self._settle(session, payout, deal, transfer, now)
            else:
                await self._notify_failure(session, payout, deal, now)
        return True

    async def _execute(self, payout: Payout, deal: Deal | None, now: datetime) -> Transfer | None:
        if payout.recipient_user_id is None:
            self._fail(payout, now, "config", "no recipient", final=True)
            return None
        code = deal.code if deal else str(payout.id)
        what = "Возврат" if payout.kind in BUYER_BOUND_PAYOUTS else "Выплата"
        try:
            return await self._client.transfer(
                user_id=payout.recipient_user_id,
                asset=Asset.USDT.value,
                amount=from_minor(payout.amount, deal.decimals if deal else 6),
                spend_id=payout.idempotency_key,
                comment=f"{what} по сделке {code} через гаранта @{self._ctx.bot_username}",
            )
        except CryptoPayError as exc:
            if exc.kind == "spend_id_used":
                return await self._lookup(payout, now)  # an earlier attempt went through
            if exc.kind == "recipient":
                self._fail(payout, now, exc.kind, exc.name, retry_in=RECIPIENT_RETRY)
            elif exc.kind == "insufficient_funds":
                self._fail(payout, now, exc.kind, exc.name, retry_in=FUNDS_RETRY)
            elif exc.kind in ("amount", "disabled", "auth"):
                self._fail(payout, now, exc.kind, exc.name, final=True)
            else:
                self._fail(payout, now, exc.kind, exc.name, retry_in=_backoff(payout.attempts))
            return None
        except httpx.HTTPError as exc:
            # The outcome is unknown: the transfer may have happened. Look it up by spend_id.
            log.warning("cryptobot_transfer_uncertain", payout_id=payout.id, error=repr(exc))
            return await self._lookup(payout, now)

    async def _lookup(self, payout: Payout, now: datetime) -> Transfer | None:
        try:
            found = await self._client.find_transfer(payout.idempotency_key)
        except (CryptoPayError, httpx.HTTPError) as exc:
            self._fail(payout, now, PAYOUT_OUTCOME_UNKNOWN, repr(exc), retry_in=_backoff(payout.attempts))
            return None
        if found is None:
            # Not executed: retrying with the same spend_id is safe.
            self._fail(
                payout, now, "uncertain", "transfer not found, will retry", retry_in=_backoff(payout.attempts)
            )
        return found

    def _fail(
        self,
        payout: Payout,
        now: datetime,
        kind: str,
        error: str,
        *,
        retry_in: timedelta | None = None,
        final: bool = False,
    ) -> None:
        payout.last_error = f"{kind}: {error}"[:500]
        if final or payout.attempts >= MAX_PAYOUT_ATTEMPTS:
            payout.status = PayoutStatus.FAILED_FINAL
            payout.next_attempt_at = None
        else:
            payout.status = PayoutStatus.FAILED_RETRYABLE
            payout.next_attempt_at = now + (retry_in or _backoff(payout.attempts))
        log.warning(
            "cryptobot_payout_failed",
            payout_id=payout.id,
            error=payout.last_error,
            status=payout.status.value,
        )

    async def _settle(
        self, session: AsyncSession, payout: Payout, deal: Deal | None, transfer: Transfer, now: datetime
    ) -> None:
        payout.status = PayoutStatus.CONFIRMED
        payout.external_id = str(transfer.transfer_id)
        payout.confirmed_at = now
        payout.last_error = None
        payout.next_attempt_at = None
        asset = asset_code(Asset.USDT, Network.CRYPTOBOT)
        if payout.kind is PayoutKind.UNIDENTIFIED_RETURN:
            entries = postings.unidentified_return_settled(
                asset=asset, wallet=CRYPTOBOT_WALLET, amount=payout.amount
            )
        else:
            if payout.deal_id is None:
                raise RuntimeError("deal payout without a deal")
            entries = postings.payout_settled(
                asset=asset, wallet=CRYPTOBOT_WALLET, deal_id=payout.deal_id, amount=payout.amount
            )
        await Ledger(session).post(
            idempotency_key=f"payout-settled:{payout.id}",
            kind="payout_settled",
            deal_id=payout.deal_id,
            ref_type="payout",
            ref_id=payout.id,
            postings=entries,
        )
        log.info("cryptobot_payout_confirmed", payout_id=payout.id, transfer_id=transfer.transfer_id)
        if deal is not None:
            await DealService(
                session,
                self._ctx.settings,
                self._ctx.chains,
                gateway=self._ctx.gateway,
                clock=self._ctx.clock,
            ).on_payout_settled(deal.id)

    async def _notify_failure(
        self, session: AsyncSession, payout: Payout, deal: Deal | None, now: datetime
    ) -> None:
        recipient_problem = (payout.last_error or "").startswith("recipient:")
        if (
            payout.status is PayoutStatus.FAILED_RETRYABLE
            and recipient_problem
            and payout.recipient_user_id is not None
        ):
            bot = "@CryptoTestnetBot" if self._ctx.settings.cryptobot_testnet else "@CryptoBot"
            await notifications.enqueue(
                session,
                user_id=payout.recipient_user_id,
                kind=Notice.PAYOUT_ACTION_NEEDED,
                deal_id=payout.deal_id,
                dedupe_key=f"payout-action:{payout.id}",
                now=now,
                payload={
                    "hint": f"Откройте {bot} и нажмите «Start» — выплата придёт автоматически "
                    "в течение получаса."
                },
            )
        if payout.status is PayoutStatus.FAILED_FINAL or payout.attempts in (1, 5):
            for admin_id in await notifications.admin_ids(session):
                await notifications.enqueue(
                    session,
                    user_id=admin_id,
                    kind=Notice.ADMIN_PAYOUT_FAILED,
                    deal_id=payout.deal_id,
                    dedupe_key=f"payout-failed:{payout.id}:{payout.attempts}:{admin_id}",
                    now=now,
                    payload={
                        "payout_id": payout.id,
                        "error": payout.last_error or "",
                        "status": payout.status.value,
                    },
                )


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 3600))


async def run_poller(ctx: BotContext, client: CryptoPayClient, stop: asyncio.Event) -> None:
    log.info("cryptobot_poller_started")
    while not stop.is_set():
        try:
            await poll_invoices(ctx, client)
        except Exception as exc:
            log.error("cryptobot_poll_failed", error=repr(exc), exc_info=exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            continue


async def run_payouts(ctx: BotContext, client: CryptoPayClient, stop: asyncio.Event) -> None:
    executor = CryptoBotPayouts(ctx, client)
    log.info("cryptobot_payouts_started")
    while not stop.is_set():
        try:
            busy = await executor.run_once()
        except Exception as exc:
            log.error("cryptobot_payout_loop_error", error=repr(exc), exc_info=exc)
            busy = False
        if busy:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=PAYOUT_IDLE_SECONDS)
        except TimeoutError:
            continue
