"""Routes payment requests to the provider of the deal (CryptoBot now, on-chain networks later)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, PaymentRequest
from app.domain.enums import Network
from app.domain.errors import ProviderUnavailable
from app.services.deals import PaymentGateway


class RoutingGateway:
    def __init__(self, gateways: Mapping[Network, PaymentGateway]) -> None:
        self._gateways = dict(gateways)

    def _for(self, network: Network) -> PaymentGateway:
        gateway = self._gateways.get(network)
        if gateway is None:
            raise ProviderUnavailable("payment for this network is not available yet", network=network)
        return gateway

    async def create_request(self, session: AsyncSession, deal: Deal, *, now: datetime) -> PaymentRequest:
        return await self._for(deal.network).create_request(session, deal, now=now)

    async def extend_request(
        self, session: AsyncSession, request: PaymentRequest, *, new_expires_at: datetime
    ) -> bool:
        return await self._for(request.network).extend_request(
            session, request, new_expires_at=new_expires_at
        )

    async def cancel_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        gateway = self._gateways.get(request.network)
        if gateway is not None:
            await gateway.cancel_request(session, request)

    async def sync_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        gateway = self._gateways.get(request.network)
        if gateway is not None:
            await gateway.sync_request(session, request)
