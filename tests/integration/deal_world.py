"""Test harness for the deal service: controllable clock, fake payment gateway, money helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ChainsConfig, Settings
from app.db.models import Deal, DealEvent, NotificationOutbox, PaymentRequest, Payout
from app.domain.enums import (
    DealSource,
    FeePayer,
    Network,
    PartyRole,
    PaymentRequestStatus,
    PayoutStatus,
    Provider,
    UserRole,
)
from app.domain.money import asset_code
from app.services import ledger as L
from app.services.deals import DealService, NewDeal
from app.services.ledger import AccountRef, Ledger
from tests import addresses
from tests.factories import make_user

BUYER, SELLER, STRANGER, ADMIN = 101, 202, 303, 909
CRYPTOBOT_WALLET = "cryptobot-app"
SELLER_ADDRESSES = {
    Network.TRON: addresses.tron(7),
    Network.BSC: addresses.evm(7),
    Network.ETH: addresses.evm(7),
    Network.TON: addresses.ton(7),
    Network.SOL: addresses.sol(7),
}


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 3, 1, 10, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class FakeGateway:
    settings: Settings
    chains: ChainsConfig
    extendable: bool = True
    cancelled: list[int] = field(default_factory=list)

    async def create_request(self, session: AsyncSession, deal: Deal, *, now: datetime) -> PaymentRequest:
        onchain = deal.provider is Provider.ONCHAIN
        request = PaymentRequest(
            deal_id=deal.id,
            provider=deal.provider,
            network=deal.network,
            asset=deal.asset,
            expected_amount=deal.buyer_pays,
            deposit_address=self.chains.networks[deal.network].deposit_address if onchain else None,
            external_id=None if onchain else f"inv-{deal.id}",
            status=PaymentRequestStatus.ACTIVE,
            tag_reserved=onchain,
            expires_at=now + self.settings.payment_ttl,
        )
        session.add(request)
        await session.flush()
        return request

    async def extend_request(
        self, session: AsyncSession, request: PaymentRequest, *, new_expires_at: datetime
    ) -> bool:
        return self.extendable

    async def cancel_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        self.cancelled.append(request.id)

    async def sync_request(self, session: AsyncSession, request: PaymentRequest) -> None:
        return None


@dataclass
class World:
    session: AsyncSession
    settings: Settings
    chains: ChainsConfig
    clock: Clock = field(default_factory=Clock)
    gateway: FakeGateway | None = None
    _deposits: int = 0

    def __post_init__(self) -> None:
        self.gateway = FakeGateway(self.settings, self.chains)

    @property
    def svc(self) -> DealService:
        return DealService(self.session, self.settings, self.chains, gateway=self.gateway, clock=self.clock)

    async def setup_users(self, *, experienced: bool = False) -> None:
        completed = 10 if experienced else 0
        await make_user(self.session, BUYER, "alice", deals_completed=completed)
        await make_user(self.session, SELLER, "bob", deals_completed=completed)
        await make_user(self.session, STRANGER, "mallory")
        await make_user(self.session, ADMIN, "admin", role=UserRole.ADMIN)

    async def new_deal(
        self,
        *,
        network: Network = Network.CRYPTOBOT,
        amount: str = "100",
        creator_role: PartyRole = PartyRole.BUYER,
        fee_payer: FeePayer = FeePayer.BUYER,
        counterparty: int | None = None,
        counterparty_username: str | None = None,
        payout_address: str | None = None,
        source: DealSource = DealSource.WIZARD,
    ) -> Deal:
        creator = BUYER if creator_role is PartyRole.BUYER else SELLER
        if counterparty is None and counterparty_username is None:
            counterparty = SELLER if creator_role is PartyRole.BUYER else BUYER
        if (
            creator_role is PartyRole.SELLER
            and network.provider is Provider.ONCHAIN
            and payout_address is None
        ):
            payout_address = SELLER_ADDRESSES[network]
        return await self.svc.create_deal(
            NewDeal(
                creator_id=creator,
                creator_role=creator_role,
                network=network,
                amount=Decimal(amount),
                terms="Аккаунт с играми, передача логина и почты",
                execution_hours=24,
                fee_payer=fee_payer,
                counterparty_user_id=counterparty,
                counterparty_username=counterparty_username,
                seller_payout_address=payout_address,
                source=source,
            )
        )

    async def deposit(self, deal: Deal, amount: int, *, wallet: str | None = None) -> None:
        self._deposits += 1
        wallet = wallet or (CRYPTOBOT_WALLET if deal.provider is Provider.CRYPTOBOT else "hot")
        await Ledger(self.session).post(
            idempotency_key=f"test-deposit:{deal.id}:{self._deposits}",
            kind="deposit",
            postings=L.deposit_matched(
                asset=asset_code(deal.asset, deal.network), wallet=wallet, deal_id=deal.id, amount=amount
            ),
            deal_id=deal.id,
        )

    async def funded_deal(self, **kw: object) -> Deal:
        """CREATED → ACCEPTED → AWAITING_PAYMENT → FUNDED (exact payment)."""
        deal = await self.new_deal(**kw)  # type: ignore[arg-type]
        counterparty = SELLER if deal.creator_role is PartyRole.BUYER else BUYER
        seller_address = None
        if deal.provider is Provider.ONCHAIN and counterparty == SELLER:
            seller_address = SELLER_ADDRESSES[deal.network]
        await self.svc.accept_deal(counterparty, deal.id, version=deal.version, payout_address=seller_address)
        request = await self.svc.start_payment(BUYER, deal.id)
        await self.deposit(deal, request.expected_amount)
        await self.svc.fund(deal.id)
        return deal

    async def escrow(self, deal: Deal) -> int:
        return await Ledger(self.session).normal_balance(
            AccountRef.escrow(asset_code(deal.asset, deal.network), deal.id)
        )

    async def payable(self, deal: Deal) -> int:
        return await Ledger(self.session).normal_balance(
            AccountRef.payable(asset_code(deal.asset, deal.network), deal.id)
        )

    async def payouts(self, deal: Deal) -> list[Payout]:
        rows = await self.session.execute(
            sa.select(Payout).where(Payout.deal_id == deal.id).order_by(Payout.id)
        )
        return list(rows.scalars())

    async def settle_payouts(self, deal: Deal) -> None:
        """Pretend the payout worker executed every queued payout."""
        for payout in await self.payouts(deal):
            if payout.status is PayoutStatus.QUEUED:
                payout.status = PayoutStatus.CONFIRMED
                await Ledger(self.session).post(
                    idempotency_key=f"settled:{payout.id}",
                    kind="payout_settled",
                    postings=L.payout_settled(
                        asset=asset_code(deal.asset, deal.network),
                        wallet=CRYPTOBOT_WALLET if deal.provider is Provider.CRYPTOBOT else "hot",
                        deal_id=deal.id,
                        amount=payout.amount,
                    ),
                    deal_id=deal.id,
                )
        await self.svc.on_payout_settled(deal.id)

    async def events(self, deal: Deal) -> list[DealEvent]:
        rows = await self.session.execute(
            sa.select(DealEvent).where(DealEvent.deal_id == deal.id).order_by(DealEvent.seq)
        )
        return list(rows.scalars())

    async def notices(self, user_id: int) -> list[str]:
        rows = await self.session.execute(
            sa.select(NotificationOutbox.kind)
            .where(NotificationOutbox.user_id == user_id)
            .order_by(NotificationOutbox.id)
        )
        return list(rows.scalars())

    async def run_due_timers(self) -> int:
        processed = 0
        for deal_id in await self.svc.due_deal_ids():
            processed += int(await self.svc.process_timer(deal_id))
        return processed
