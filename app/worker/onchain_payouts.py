"""On-chain payouts: EVM networks (BEP-20, ERC-20) and TRC-20, docs §9.1.

Three steps, each its own short DB transaction, so a crash anywhere is safe:

1. **sign** a queued payout: preflight (ledger owes it, the hot wallet holds the USDT and the gas,
   payouts are not paused), a nonce from ``hot_wallets`` under a row lock, the signer builds and
   signs the transaction from the payout row, and the attempt (raw tx, hash, nonce) is stored
   **before** anything is sent → ``signing``;
2. **broadcast** the stored transaction to every endpoint → ``broadcast``. A retry re-sends the
   very same signed bytes; a payout is never signed twice while an attempt may still land;
3. **track** until the block is final at both sources: success with our ``Transfer`` log →
   ``confirmed``, ledger and the deal move on; a revert → ``failed_final`` and an alert (the nonce
   is spent, nothing was paid). Gas is booked either way, in the native coin.

A transaction that does not appear is re-broadcast; after ``STUCK_ALERT_AFTER`` admins are told
(fee too low: replacing it with a higher fee is a manual decision for now).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.context import BotContext
from app.db.models import HotWallet, Payout, PayoutAttempt, PayoutPause
from app.domain.enums import Network, PayoutAttemptStatus, PayoutKind, PayoutStatus, Provider
from app.domain.money import asset_code, native_asset_code, to_minor
from app.logging import get_logger
from app.services import ledger as postings
from app.services import notifications
from app.services.deals import DealService
from app.services.ledger import AccountRef, Ledger
from app.services.notifications import Notice
from app.signer.signer import EvmFees, SignedTx, SignerRefused, SigningService

log = get_logger(__name__)

GAS_LIMIT = 100_000  # a USDT transfer costs ~50–65k gas; the unused part is not charged
TRACK_EVERY = timedelta(seconds=15)
BROADCAST_RETRY = timedelta(seconds=30)
FUNDS_RETRY = timedelta(minutes=10)
STUCK_ALERT_AFTER = timedelta(minutes=30)
IDLE_SECONDS = 5.0
IN_FLIGHT = (PayoutStatus.SIGNING, PayoutStatus.BROADCAST)
EXPIRY_MARGIN_MS = 2 * 60 * 1000  # TRON: re-sign only this long after the old transaction expired


@dataclass(frozen=True)
class Outcome:
    state: Literal["pending", "missing", "expired", "ok", "failed"]
    fee: int = 0  # native coin actually spent (gas / burnt TRX)


class OnchainPayouts:
    """The network-independent part; subclasses sign, price gas and read outcomes."""

    def __init__(self, ctx: BotContext, adapter: Any, signer: SigningService) -> None:
        self._ctx = ctx
        self._adapter = adapter
        self._signer = signer
        self.network: Network = adapter.network
        self._hot = adapter.addresses[0]
        self._asset = asset_code("USDT", self.network)
        self._native = native_asset_code(self.network)

    async def run_once(self) -> bool:
        return await self.sign_next() or await self.broadcast_next() or await self.track_next()

    # ── 1. sign ────────────────────────────────────────────────────────────
    async def sign_next(self) -> bool:
        now = self._ctx.clock()
        async with self._ctx.sessionmaker() as s, s.begin():
            if await s.get(PayoutPause, self._hot) is not None:
                return False
            payout = await self._claim(s, [PayoutStatus.QUEUED, PayoutStatus.FAILED_RETRYABLE], now)
            if payout is None:
                return False
            payout.attempts += 1
            problem = await self._preflight(s, payout)
            if problem is not None:
                kind, text, final = problem
                await self._fail(s, payout, now, kind, text, final=final)
                return True
            wallet = await self._hot_wallet(s)
            try:
                signed = await self._sign(s, payout, wallet)
            except SignerRefused as exc:
                await self._fail(s, payout, now, "signer", exc.message, final=True)
                return True
            attempt_no = int(
                await s.scalar(
                    sa.select(sa.func.coalesce(sa.func.max(PayoutAttempt.attempt_no), 0)).where(
                        PayoutAttempt.payout_id == payout.id
                    )
                )
                or 0
            )
            s.add(
                PayoutAttempt(
                    payout_id=payout.id,
                    attempt_no=attempt_no + 1,
                    tx_hash=signed.tx_hash,
                    raw_tx=signed.raw,
                    nonce=signed.nonce,
                    valid_until=signed.valid_until,
                    status=PayoutAttemptStatus.SIGNED,
                )
            )
            payout.status = PayoutStatus.SIGNING
            payout.hot_wallet_id = wallet.id
            payout.next_attempt_at = now
            payout.last_error = None
        return True

    async def _preflight(self, s: AsyncSession, payout: Payout) -> tuple[str, str, bool] | None:
        """(kind, text, final) when the payout must not be signed now."""
        ledger = Ledger(s)
        owed_account = (
            AccountRef.payable_unidentified(self._asset)
            if payout.kind is PayoutKind.UNIDENTIFIED_RETURN
            else AccountRef.payable(self._asset, payout.deal_id or 0)
        )
        if await ledger.normal_balance(owed_account) < payout.amount:
            return ("ledger", "the ledger does not owe this amount", True)
        in_flight = int(
            await s.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(Payout.amount), 0)).where(
                    Payout.network == self.network, Payout.status.in_(IN_FLIGHT)
                )
            )
            or 0
        )
        if await self._adapter.token_balance(self._hot) < payout.amount + in_flight:
            return ("insufficient_funds", "the hot wallet holds less USDT than the payout", False)
        if await self._adapter.native_balance(self._hot) < await self._gas_needed():
            return ("no_gas", f"the hot wallet needs {self._native} for gas", False)
        return None

    async def _sign(self, s: AsyncSession, payout: Payout, wallet: HotWallet) -> SignedTx:
        raise NotImplementedError

    async def _gas_needed(self) -> int:
        raise NotImplementedError

    async def _init_wallet(self, wallet: HotWallet) -> None:
        return None

    async def _outcome(self, s: AsyncSession, payout: Payout, attempt: PayoutAttempt) -> Outcome:
        raise NotImplementedError

    async def _fees(self) -> EvmFees:
        if self.network is Network.ETH:
            max_fee, tip = await self._adapter.fee_caps()
            return EvmFees(gas=GAS_LIMIT, max_fee=max_fee, max_priority_fee=tip)
        return EvmFees(gas=GAS_LIMIT, gas_price=await self._adapter.gas_price())

    async def _hot_wallet(self, s: AsyncSession) -> HotWallet:
        await s.execute(
            pg_insert(HotWallet)
            .values(network=self.network, address=self._hot)
            .on_conflict_do_nothing(constraint="uq_hot_wallets_network_address")
        )
        wallet = (
            await s.execute(
                sa.select(HotWallet)
                .where(HotWallet.network == self.network, HotWallet.address == self._hot)
                .with_for_update()
            )
        ).scalar_one()
        await self._init_wallet(wallet)
        return wallet

    # ── 2. broadcast ───────────────────────────────────────────────────────
    async def broadcast_next(self) -> bool:
        now = self._ctx.clock()
        async with self._ctx.sessionmaker() as s, s.begin():
            payout = await self._claim(s, [PayoutStatus.SIGNING], now)
            if payout is None:
                return False
            attempt = await self._latest_attempt(s, payout)
            if attempt is None or attempt.raw_tx is None:  # pragma: no cover - signing stores both
                await self._fail(s, payout, now, "internal", "signed attempt is missing", final=True)
                return True
            if await self._adapter.broadcast(attempt.raw_tx):
                attempt.status = PayoutAttemptStatus.BROADCAST
                payout.status = PayoutStatus.BROADCAST
                payout.next_attempt_at = now + TRACK_EVERY
                log.info("onchain_payout_broadcast", payout_id=payout.id, tx_hash=attempt.tx_hash)
            else:
                payout.last_error = "broadcast: no endpoint accepted the transaction (will resend it)"
                payout.next_attempt_at = now + BROADCAST_RETRY
        return True

    # ── 3. track ───────────────────────────────────────────────────────────
    async def track_next(self) -> bool:
        now = self._ctx.clock()
        async with self._ctx.sessionmaker() as s, s.begin():
            payout = await self._claim(s, [PayoutStatus.BROADCAST], now)
            if payout is None:
                return False
            attempt = await self._latest_attempt(s, payout)
            assert attempt is not None and attempt.tx_hash is not None  # noqa: S101 - broadcast implies it
            payout.next_attempt_at = now + TRACK_EVERY
            outcome = await self._outcome(s, payout, attempt)
            if outcome.state == "pending":
                return True
            if outcome.state == "missing":
                await self._not_mined(s, payout, attempt, now)
                return True
            if outcome.state == "expired":
                # Provably dead (past its expiry, unknown to both sources): safe to sign anew.
                attempt.status = PayoutAttemptStatus.EXPIRED
                payout.status = PayoutStatus.QUEUED
                payout.next_attempt_at = now
                payout.last_error = f"expired: {attempt.tx_hash} never landed, signing again"
                log.warning("onchain_payout_expired", payout_id=payout.id, tx_hash=attempt.tx_hash)
                return True
            if outcome.fee:
                await self._book_gas(s, attempt, outcome.fee)
            payout.native_fee_spent = outcome.fee
            if outcome.state == "ok":
                await self._settle(s, payout, attempt, now)
            else:
                attempt.status = PayoutAttemptStatus.REVERTED
                await self._fail(
                    s, payout, now, "reverted", f"transaction {attempt.tx_hash} failed on chain", final=True
                )
        return True

    async def _not_mined(
        self, s: AsyncSession, payout: Payout, attempt: PayoutAttempt, now: datetime
    ) -> None:
        if attempt.raw_tx is not None:
            await self._adapter.broadcast(attempt.raw_tx)  # same bytes: harmless if it is already pending
        age = await s.scalar(
            sa.select(sa.func.now() - PayoutAttempt.created_at).where(PayoutAttempt.id == attempt.id)
        )
        if isinstance(age, timedelta) and age > STUCK_ALERT_AFTER:
            await self._alert(
                s, payout, now, f"transaction {attempt.tx_hash} is not mined for {age}", "stuck"
            )

    async def _settle(self, s: AsyncSession, payout: Payout, attempt: PayoutAttempt, now: datetime) -> None:
        attempt.status = PayoutAttemptStatus.FINAL_OK
        payout.status = PayoutStatus.CONFIRMED
        payout.confirmed_tx_hash = attempt.tx_hash
        payout.confirmed_at = now
        payout.next_attempt_at = None
        payout.last_error = None
        if payout.kind is PayoutKind.UNIDENTIFIED_RETURN:
            entries = postings.unidentified_return_settled(
                asset=self._asset, wallet=self._hot, amount=payout.amount
            )
        else:
            entries = postings.payout_settled(
                asset=self._asset, wallet=self._hot, deal_id=payout.deal_id or 0, amount=payout.amount
            )
        await Ledger(s).post(
            idempotency_key=f"payout-settled:{payout.id}",
            kind="payout_settled",
            deal_id=payout.deal_id,
            ref_type="payout",
            ref_id=payout.id,
            postings=entries,
        )
        log.info("onchain_payout_confirmed", payout_id=payout.id, tx_hash=attempt.tx_hash)
        if payout.deal_id is not None:
            await DealService(
                s, self._ctx.settings, self._ctx.chains, gateway=self._ctx.gateway, clock=self._ctx.clock
            ).on_payout_settled(payout.deal_id)

    async def _book_gas(self, s: AsyncSession, attempt: PayoutAttempt, gas: int) -> None:
        """Gas in the native coin. If the gas top-up was never recorded (record-funding), the cost is
        booked against equity instead of driving the ledger's custody below zero."""
        ledger = Ledger(s)
        if await ledger.normal_balance(AccountRef.custody(self._native, self._hot)) >= gas:
            entries = postings.network_fee_spent(native_asset=self._native, wallet=self._hot, amount=gas)
        else:
            entries = [
                postings.debit(AccountRef.network_fee_expense(self._native), gas),
                postings.credit(AccountRef.adjustments(self._native), gas),
            ]
        await ledger.post(
            idempotency_key=f"payout-gas:{attempt.payout_id}:{attempt.attempt_no}",
            kind="network_fee",
            ref_type="payout",
            ref_id=attempt.payout_id,
            postings=entries,
        )

    # ── helpers ────────────────────────────────────────────────────────────
    async def _claim(self, s: AsyncSession, statuses: list[PayoutStatus], now: datetime) -> Payout | None:
        return (
            await s.execute(
                sa.select(Payout)
                .where(
                    Payout.provider == Provider.ONCHAIN,
                    Payout.network == self.network,
                    Payout.status.in_(statuses),
                    Payout.next_attempt_at <= now,
                )
                .order_by(Payout.id)
                .limit(1)
                # NO KEY UPDATE: we change no key columns, and the signer's log row references this
                # payout — a plain FOR UPDATE would block that foreign-key check while we wait for it.
                .with_for_update(skip_locked=True, key_share=True)
            )
        ).scalar_one_or_none()

    async def _latest_attempt(self, s: AsyncSession, payout: Payout) -> PayoutAttempt | None:
        return (
            await s.execute(
                sa.select(PayoutAttempt)
                .where(PayoutAttempt.payout_id == payout.id)
                .order_by(PayoutAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _fail(
        self, s: AsyncSession, payout: Payout, now: datetime, kind: str, text: str, *, final: bool
    ) -> None:
        payout.last_error = f"{kind}: {text}"[:500]
        if final:
            payout.status = PayoutStatus.FAILED_FINAL
            payout.next_attempt_at = None
        else:
            payout.status = PayoutStatus.FAILED_RETRYABLE
            payout.next_attempt_at = now + FUNDS_RETRY
        log.warning(
            "evm_payout_failed", payout_id=payout.id, error=payout.last_error, status=payout.status.value
        )
        if final or payout.attempts in (1, 5):
            await self._alert(s, payout, now, payout.last_error, kind)

    async def _alert(self, s: AsyncSession, payout: Payout, now: datetime, error: str, kind: str) -> None:
        for admin_id in await notifications.admin_ids(s):
            await notifications.enqueue(
                s,
                user_id=admin_id,
                kind=Notice.ADMIN_PAYOUT_FAILED,
                deal_id=payout.deal_id,
                dedupe_key=f"onchain-payout:{payout.id}:{kind}:{payout.attempts}:{admin_id}"[:128],
                now=now,
                payload={"payout_id": payout.id, "error": error[:300], "status": payout.status.value},
            )


class EvmPayouts(OnchainPayouts):
    async def _init_wallet(self, wallet: HotWallet) -> None:
        if wallet.next_nonce is None:
            wallet.next_nonce = await self._adapter.nonce(self._hot)

    async def _gas_needed(self) -> int:
        return GAS_LIMIT * int(await self._adapter.gas_price())

    async def _sign(self, s: AsyncSession, payout: Payout, wallet: HotWallet) -> SignedTx:
        nonce = wallet.next_nonce
        assert nonce is not None  # noqa: S101 - initialized by _init_wallet
        signed = await self._signer.sign_evm_transfer(
            s, payout.id, from_address=self._hot, nonce=nonce, fees=await self._fees()
        )
        wallet.next_nonce = nonce + 1  # spent even if the transaction later reverts
        return signed

    async def _outcome(self, s: AsyncSession, payout: Payout, attempt: PayoutAttempt) -> Outcome:
        assert attempt.tx_hash is not None  # noqa: S101
        receipt = await self._adapter.receipt(attempt.tx_hash)
        if receipt is None:
            return Outcome("missing")
        if int(receipt["blockNumber"], 16) > await self._adapter.final_head():
            return Outcome("pending")
        second = await self._adapter.receipt(attempt.tx_hash, second_source=True)
        if (
            second is None
            or int(second["blockNumber"], 16) > await self._adapter.final_head(second_source=True)
            or str(second.get("blockHash")).lower() != str(receipt.get("blockHash")).lower()
            or second.get("status") != receipt.get("status")
        ):
            return Outcome("pending")  # the second source has not caught up (or disagrees): never guess
        fee = int(receipt.get("gasUsed", "0x0"), 16) * int(receipt.get("effectiveGasPrice", "0x0"), 16)
        ok = receipt.get("status") == "0x1" and self._adapter.transfer_in_receipt(
            receipt, to=payout.recipient_address or "", amount=payout.amount
        )
        return Outcome("ok" if ok else "failed", fee)


class TronPayouts(OnchainPayouts):
    """No nonces on TRON: a transaction carries an expiration (10 min). It is signed again only once
    the solidified head is past that expiration plus a margin and both sources do not know it."""

    async def _gas_needed(self) -> int:
        return self._fee_limit

    @property
    def _fee_limit(self) -> int:
        return to_minor(self._ctx.settings.tron_fee_limit_trx, 6)

    async def _sign(self, s: AsyncSession, payout: Payout, wallet: HotWallet) -> SignedTx:
        ref = await self._adapter.ref_block()
        return await self._signer.sign_tron_transfer(
            s,
            payout.id,
            from_address=self._hot,
            ref=ref,
            now_ms=await self._adapter.chain_time_ms(),
            fee_limit_sun=self._fee_limit,
        )

    async def _outcome(self, s: AsyncSession, payout: Payout, attempt: PayoutAttempt) -> Outcome:
        assert attempt.tx_hash is not None  # noqa: S101
        info = await self._adapter.tx_info(attempt.tx_hash)
        second = await self._adapter.tx_info(attempt.tx_hash, second_source=True)
        if not info or not second:
            if info or second:
                return Outcome("pending")  # solidified at one source only so far
            expired = attempt.valid_until is not None and (
                await self._adapter.solid_time_ms()
                > attempt.valid_until.timestamp() * 1000 + EXPIRY_MARGIN_MS
            )
            return Outcome("expired") if expired else Outcome("missing")
        if (info.get("receipt") or {}).get("result") != (second.get("receipt") or {}).get("result"):
            return Outcome("pending")
        fee = int(info.get("fee") or 0)
        ok = (info.get("receipt") or {}).get("result") == "SUCCESS" and self._adapter.transfer_in_info(
            info, to=payout.recipient_address or "", amount=payout.amount
        )
        return Outcome("ok" if ok else "failed", fee)


async def run_onchain_payouts(executor: OnchainPayouts, stop: asyncio.Event) -> None:
    log.info("evm_payouts_started", network=executor.network.value)
    while not stop.is_set():
        try:
            busy = await executor.run_once()
        except Exception as exc:  # an RPC outage must not kill the loop
            log.warning("evm_payout_loop_error", network=executor.network.value, error=repr(exc))
            busy = False
        if busy:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=IDLE_SECONDS)
        except TimeoutError:
            continue
