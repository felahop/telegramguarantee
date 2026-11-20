"""On-chain payment requests: a shared deposit address plus identifiers that tie a transfer to a deal.

* every request gets a **unique amount tag** (0.0001…0.0999 USDT, see ``domain.fees``): the
  exact amount is reserved on that address until the cooldown after the request closes;
* TON additionally gets the deal code as the **comment** (memo), SOL a random **Solana Pay
  reference** — both survive exchanges that deduct a fee and break the exact amount.

The watcher reads the chain on its own, so syncing on demand is a no-op and requests can be
extended freely (no provider-side expiry).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime

import base58
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.addresses import validate_address
from app.config import ChainsConfig, Settings
from app.db.models import Deal, PaymentRequest
from app.domain.enums import Network, PaymentRequestStatus, Provider
from app.domain.errors import LimitExceeded, ValidationFailed
from app.domain.fees import TAG_MAX_STEPS, tagged_amount
from app.logging import get_logger

log = get_logger(__name__)
TAG_ATTEMPTS = 25


class OnchainGateway:
    def __init__(
        self,
        settings: Settings,
        chains: ChainsConfig,
        *,
        clock: Callable[[], datetime],
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        self._settings = settings
        self._chains = chains
        self._clock = clock
        self._randbelow = randbelow

    async def create_request(self, session: AsyncSession, deal: Deal, *, now: datetime) -> PaymentRequest:
        if deal.provider is not Provider.ONCHAIN:
            raise ValueError("not an on-chain deal")
        cfg = self._chains.networks[deal.network]
        if cfg.deposit_mode != "shared":
            raise ValidationFailed("per-deal deposit addresses are not supported yet", network=deal.network)
        address = validate_address(
            deal.network, cfg.deposit_address, allow_testnet=self._settings.testnet_mode
        )
        for _ in range(TAG_ATTEMPTS):
            steps = 1 + self._randbelow(TAG_MAX_STEPS)
            amount, tag = tagged_amount(deal.buyer_pays, steps, deal.decimals, mode=self._settings.tag_mode)
            request = PaymentRequest(
                deal_id=deal.id,
                provider=Provider.ONCHAIN,
                network=deal.network,
                asset=deal.asset,
                expected_amount=amount,
                tag_amount=tag,
                tag_reserved=True,
                deposit_address=address,
                memo=deal.code if deal.network is Network.TON else None,
                sol_reference=_sol_reference() if deal.network is Network.SOL else None,
                status=PaymentRequestStatus.ACTIVE,
                expires_at=now + self._settings.payment_ttl,
            )
            try:
                async with session.begin_nested():
                    session.add(request)
                    await session.flush()
            except IntegrityError as exc:
                if "uq_payment_requests_tag" not in str(exc.orig):
                    raise
                continue  # this exact amount is taken on the address: draw another tag
            log.info("onchain_request_created", deal_id=deal.id, network=deal.network.value, tag=tag)
            return request
        raise LimitExceeded("no free payment tag for this amount right now, try again later")

    async def extend_request(
        self, session: AsyncSession, request: PaymentRequest, *, new_expires_at: datetime
    ) -> bool:
        return True

    async def cancel_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        return None  # the tag stays reserved for the cooldown (a late payment still finds its deal)

    async def sync_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        return None  # the ChainWatcher credits payments as soon as they are final


def _sol_reference() -> str:
    """A random 32-byte public key: wallets add it to the transaction's accounts (Solana Pay)."""
    return str(base58.b58encode(secrets.token_bytes(32)).decode())
