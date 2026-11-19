"""Applying incoming payments to deals.

CryptoBot: an invoice paid notification (webhook or polling — both call the same idempotent method)
is checked against our payment request: same invoice, USDT, exact amount, our payload. Anything
that does not match is NOT credited to a deal: the money is booked as unidentified and admins are
alerted. A second delivery of the same invoice is a no-op (unique index on the transfer).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ChainsConfig, Settings
from app.db.models import Deal, IncomingTransfer, PaymentRequest
from app.domain.enums import (
    Asset,
    DealStatus,
    MatchMethod,
    MatchStatus,
    Network,
    PaymentRequestStatus,
    Provider,
    TransferStatus,
)
from app.domain.errors import DomainError
from app.domain.money import asset_code, to_minor
from app.logging import get_logger
from app.providers.cryptobot.client import Invoice
from app.services import ledger as postings
from app.services import notifications
from app.services.deals import DealService, PaymentGateway, utcnow
from app.services.ledger import Ledger
from app.services.notifications import Notice

log = get_logger(__name__)

CRYPTOBOT_WALLET = "cryptobot"
LATE_PAYMENT_STATUSES = frozenset({DealStatus.EXPIRED, DealStatus.CANCELLED, DealStatus.REFUND_PENDING})


class ApplyResult(StrEnum):
    FUNDED = "funded"
    LATE = "late"
    DUPLICATE = "duplicate"
    ANOMALY = "anomaly"
    NOT_PAID = "not_paid"


def invoice_payload(request: PaymentRequest) -> str:
    return f"pr:{request.id}:d:{request.deal_id}"


class PaymentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        chains: ChainsConfig,
        *,
        gateway: PaymentGateway | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._s = session
        self._settings = settings
        self._chains = chains
        self._gateway = gateway
        self._clock = clock
        self._ledger = Ledger(session)

    @property
    def deals(self) -> DealService:
        return DealService(self._s, self._settings, self._chains, gateway=self._gateway, clock=self._clock)

    async def apply_cryptobot_invoice(self, invoice: Invoice) -> ApplyResult:
        if invoice.status != "paid":
            return ApplyResult.NOT_PAID
        request = (
            await self._s.execute(
                sa.select(PaymentRequest).where(
                    PaymentRequest.provider == Provider.CRYPTOBOT,
                    PaymentRequest.external_id == str(invoice.invoice_id),
                )
            )
        ).scalar_one_or_none()
        deal = await self._lock_deal(request.deal_id) if request is not None else None
        amount, fee, problems = self._inspect(invoice, request, deal)

        transfer_id = await self._record_transfer(invoice, amount, request, deal, matched=not problems)
        if transfer_id is None:
            log.info("cryptobot_invoice_duplicate", invoice_id=invoice.invoice_id)
            return ApplyResult.DUPLICATE
        if problems or deal is None or request is None:
            log.warning("cryptobot_invoice_anomaly", invoice_id=invoice.invoice_id, problems=problems)
            if amount > 0:
                await self._book(invoice, amount, fee, None, transfer_id)
            await self._alert(deal, {"invoice_id": invoice.invoice_id, "problems": ",".join(problems)})
            return ApplyResult.ANOMALY

        await self._book(invoice, amount, fee, deal.id, transfer_id)
        tx_ref = f"cryptobot:{invoice.invoice_id}"
        if deal.status is DealStatus.AWAITING_PAYMENT:
            await self.deals.fund(deal.id, tx_hash=tx_ref)
            log.info("deal_funded", deal_id=deal.id, invoice_id=invoice.invoice_id)
            return ApplyResult.FUNDED
        request.status = PaymentRequestStatus.PAID
        await self.deals.on_late_payment(deal.id, tx_hash=tx_ref)
        log.info("late_payment_refund", deal_id=deal.id, invoice_id=invoice.invoice_id)
        return ApplyResult.LATE

    def _inspect(
        self, invoice: Invoice, request: PaymentRequest | None, deal: Deal | None
    ) -> tuple[int, int, list[str]]:
        """(amount, provider fee, problems). Any problem means: do not credit a deal."""
        decimals = self._chains.spec(Network.CRYPTOBOT, include_disabled=True).decimals
        problems: list[str] = []
        if request is None or deal is None:
            problems.append("unknown_invoice")
        if invoice.received_asset != Asset.USDT.value:
            problems.append(f"asset:{invoice.received_asset}")
        try:
            amount = to_minor(invoice.received_amount, decimals)
        except DomainError:  # more precision than USDT has: certainly not our invoice amount
            amount = 0
            problems.append("amount_precision")
        if request is not None:
            if amount != request.expected_amount:
                problems.append("amount_mismatch")
            if invoice.payload != invoice_payload(request):
                problems.append("payload_mismatch")
        fee = 0
        if invoice.fee_amount and invoice.fee_asset in (None, Asset.USDT.value):
            # A provider fee is our expense, not the buyer's money: rounding it to USDT precision
            # (instead of failing on an unexpected number of decimals) is the safe choice.
            quantum = Decimal(1).scaleb(-decimals)
            fee = to_minor(invoice.fee_amount.quantize(quantum, rounding=ROUND_HALF_UP), decimals)
            if fee >= amount > 0:
                problems.append("fee_exceeds_amount")
                fee = 0
        if request is not None and deal is not None and not problems:
            awaiting = (
                deal.status is DealStatus.AWAITING_PAYMENT and request.status is PaymentRequestStatus.ACTIVE
            )
            if not awaiting and deal.status not in LATE_PAYMENT_STATUSES:
                problems.append(f"unexpected_deal_status:{deal.status.value}")
        return amount, fee, problems

    async def _book(
        self, invoice: Invoice, amount: int, fee: int, deal_id: int | None, transfer_id: int
    ) -> None:
        """Ledger: escrow of the deal (or unidentified when ``deal_id`` is None)."""
        await self._ledger.post(
            idempotency_key=f"cryptobot-invoice:{invoice.invoice_id}",
            kind="deposit" if deal_id is not None else "deposit_unidentified",
            deal_id=deal_id,
            ref_type="incoming_transfer",
            ref_id=transfer_id,
            postings=postings.provider_deposit(
                asset=asset_code(Asset.USDT, Network.CRYPTOBOT),
                wallet=CRYPTOBOT_WALLET,
                amount=amount,
                provider_fee=fee,
                deal_id=deal_id,
            ),
        )

    async def _lock_deal(self, deal_id: int) -> Deal | None:
        return (
            await self._s.execute(
                sa.select(Deal)
                .where(Deal.id == deal_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _record_transfer(
        self,
        invoice: Invoice,
        amount: int,
        request: PaymentRequest | None,
        deal: Deal | None,
        *,
        matched: bool,
    ) -> int | None:
        """Insert the payment once; returns None if this invoice was already recorded."""
        now = self._clock()
        inserted = await self._s.execute(
            pg_insert(IncomingTransfer)
            .values(
                provider=Provider.CRYPTOBOT,
                network=Network.CRYPTOBOT,
                asset=Asset.USDT,
                tx_hash=str(invoice.invoice_id),
                event_index=0,
                to_address=CRYPTOBOT_WALLET,
                amount=max(amount, 1),  # unparseable amounts are kept (for review) as a 1-unit marker
                status=TransferStatus.FINAL if matched else TransferStatus.NEEDS_REVIEW,
                match_status=MatchStatus.MATCHED if matched else MatchStatus.UNIDENTIFIED,
                match_method=MatchMethod.INVOICE if matched else None,
                payment_request_id=request.id if request is not None and matched else None,
                deal_id=deal.id if deal is not None and matched else None,
                verified_by=["cryptopay_api"],
                raw=invoice.model_dump(mode="json"),
                first_seen_at=now,
                finalized_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_incoming_transfers_event")
            .returning(IncomingTransfer.id)
        )
        return inserted.scalar_one_or_none()

    async def _alert(self, deal: Deal | None, details: dict[str, object]) -> None:
        for admin_id in await notifications.admin_ids(self._s):
            await notifications.enqueue(
                self._s,
                user_id=admin_id,
                kind=Notice.ADMIN_PAYMENT_ANOMALY,
                deal_id=deal.id if deal is not None else None,
                dedupe_key=f"anomaly:{details.get('invoice_id')}:{admin_id}",
                payload=dict(details),
                now=self._clock(),
            )
