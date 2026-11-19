"""CryptoBot payment requests: invoices for deals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ChainsConfig, Settings
from app.db.models import Deal, PaymentRequest
from app.domain.enums import Asset, Network, PaymentRequestStatus, Provider
from app.domain.errors import ProviderUnavailable
from app.domain.money import from_minor
from app.logging import get_logger
from app.providers.cryptobot.client import CryptoPayClient, CryptoPayError

log = get_logger(__name__)


class CryptoBotGateway:
    def __init__(
        self,
        client: CryptoPayClient,
        settings: Settings,
        chains: ChainsConfig,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._settings = settings
        self._chains = chains
        self._clock = clock

    async def create_request(self, session: AsyncSession, deal: Deal, *, now: datetime) -> PaymentRequest:
        from app.services.payments import invoice_payload  # noqa: PLC0415 - avoid an import cycle

        if deal.provider is not Provider.CRYPTOBOT:
            raise ValueError("not a CryptoBot deal")
        ttl = self._settings.payment_ttl
        request = PaymentRequest(
            deal_id=deal.id,
            provider=Provider.CRYPTOBOT,
            network=Network.CRYPTOBOT,
            asset=Asset.USDT,
            expected_amount=deal.buyer_pays,
            status=PaymentRequestStatus.ACTIVE,
            tag_reserved=False,
            expires_at=now + ttl,
        )
        session.add(request)
        await session.flush()  # the request id goes into the invoice payload
        try:
            invoice = await self._client.create_invoice(
                asset=Asset.USDT.value,
                amount=from_minor(deal.buyer_pays, deal.decimals),
                description=f"Оплата сделки {deal.code} через гаранта @{self._settings.bot_username}",
                payload=invoice_payload(request),
                expires_in=int(ttl.total_seconds()),
            )
        except (CryptoPayError, httpx.HTTPError) as exc:
            log.error("cryptobot_invoice_failed", deal_id=deal.id, error=repr(exc))
            raise ProviderUnavailable("CryptoBot is not available right now") from exc
        request.external_id = str(invoice.invoice_id)
        request.pay_url = invoice.link
        log.info("cryptobot_invoice_created", deal_id=deal.id, invoice_id=invoice.invoice_id)
        return request

    async def extend_request(
        self, session: AsyncSession, request: PaymentRequest, *, new_expires_at: datetime
    ) -> bool:
        return False  # an invoice's lifetime is fixed at creation

    async def cancel_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        if not request.external_id:
            return
        try:
            await self._client.delete_invoice(int(request.external_id))
        except (CryptoPayError, httpx.HTTPError) as exc:
            # Not fatal: the invoice expires by itself; if it is paid anyway, it becomes a late
            # payment and is refunded.
            log.warning("cryptobot_invoice_delete_failed", invoice_id=request.external_id, error=repr(exc))

    async def sync_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        from app.services.payments import PaymentService  # noqa: PLC0415 - avoid an import cycle

        if not request.external_id:
            return
        try:
            invoices = await self._client.get_invoices([int(request.external_id)])
        except (CryptoPayError, httpx.HTTPError) as exc:
            log.warning("cryptobot_sync_failed", invoice_id=request.external_id, error=repr(exc))
            return
        for invoice in invoices:
            if invoice.status == "paid":
                await PaymentService(
                    session, self._settings, self._chains, gateway=self, clock=self._clock
                ).apply_cryptobot_invoice(invoice)
