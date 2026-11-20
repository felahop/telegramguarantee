"""On-chain test world: deals paid to a shared deposit address, watched through a scripted adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.storage import MemoryKV
from app.chains.base import ChainHead, ChainUnavailable, ObservedTransfer
from app.chains.watcher import ChainWatcher, TickResult
from app.config import ChainsConfig, Settings
from app.db.models import Deal, IncomingTransfer, NotificationOutbox, PaymentRequest
from app.domain.enums import FeePayer, Network, PartyRole, UserRole
from app.domain.money import asset_code
from app.providers.onchain.gateway import OnchainGateway
from app.services.deals import NewDeal
from app.services.gateways import RoutingGateway
from app.services.ledger import AccountRef, Ledger
from tests.factories import make_user
from tests.integration.deal_world import ADMIN, BUYER, SELLER, SELLER_ADDRESSES, Clock


@dataclass
class FakeAdapter:
    """A chain whose transfers and heads the test scripts."""

    network: Network
    address: str
    max_span: int = 1000
    latest: int = 100
    final: int = 100
    transfers: dict[tuple[str, int], ObservedTransfer] = field(default_factory=dict)
    disputed: set[str] = field(default_factory=set)  # tx hashes the second source disagrees with
    verify_down: bool = False
    scans: list[tuple[int, int, bool]] = field(default_factory=list)

    @property
    def active_endpoint(self) -> str | None:
        return "fake-rpc"

    @property
    def addresses(self) -> tuple[str, ...]:
        return (self.address,)

    def send(
        self,
        tx: str,
        amount: int,
        *,
        at: int,
        index: int = 0,
        memo: str | None = None,
        refs: tuple[str, ...] = (),
    ) -> ObservedTransfer:
        t = ObservedTransfer(
            network=self.network,
            tx_hash=tx,
            event_index=index,
            position=at,
            token_contract="USDT",
            to_address=self.address,
            amount=amount,
            from_address="sender",
            block_number=at,
            memo=memo,
            references=refs,
        )
        self.transfers[t.key] = t
        return t

    async def head(self) -> ChainHead:
        return ChainHead(latest=self.latest, final=self.final)

    async def scan(self, after: int, upto: int, *, final: bool) -> list[ObservedTransfer]:
        self.scans.append((after, upto, final))
        limit = self.final if final else self.latest
        return sorted(
            (t for t in self.transfers.values() if after < t.position <= min(upto, limit)),
            key=lambda t: (t.position, t.key),
        )

    async def verify(self, transfer: ObservedTransfer) -> bool:
        if self.verify_down:
            raise ChainUnavailable("second source is down")
        return transfer.tx_hash not in self.disputed

    async def token_balance(self, address: str) -> int:
        return sum(t.amount for t in self.transfers.values() if t.position <= self.final)

    async def close(self) -> None:
        return None


@dataclass
class OC:
    ctx: BotContext
    clock: Clock
    adapters: dict[Network, FakeAdapter]

    def watcher(self, network: Network = Network.TRON) -> ChainWatcher:
        return ChainWatcher(
            self.ctx.sessionmaker,
            self.ctx.settings,
            self.ctx.chains,
            self.adapters[network],  # type: ignore[arg-type]
            clock=self.clock,
            gateway=self.ctx.gateway,
        )

    async def tick(self, network: Network = Network.TRON) -> TickResult:
        return await self.watcher(network).tick()

    async def deal(self, deal_id: int) -> Deal:
        async with self.ctx.sessionmaker() as s:
            deal = await s.get(Deal, deal_id)
            assert deal is not None
            return deal

    async def transfers(self) -> list[IncomingTransfer]:
        async with self.ctx.sessionmaker() as s:
            return list(
                (await s.execute(sa.select(IncomingTransfer).order_by(IncomingTransfer.id))).scalars()
            )

    async def notices(self, user_id: int) -> list[str]:
        async with self.ctx.sessionmaker() as s:
            rows = await s.execute(
                sa.select(NotificationOutbox.kind).where(NotificationOutbox.user_id == user_id)
            )
            return list(rows.scalars())

    async def balance(self, account: AccountRef) -> int:
        async with self.ctx.sessionmaker() as s:
            return await Ledger(s).normal_balance(account)

    async def awaiting(
        self, network: Network = Network.TRON, amount: str = "100"
    ) -> tuple[int, PaymentRequest]:
        async with self.ctx.uow() as uow:
            deal = await uow.deals.create_deal(
                NewDeal(
                    creator_id=BUYER,
                    creator_role=PartyRole.BUYER,
                    network=network,
                    amount=Decimal(amount),
                    terms="Дизайн упаковки, три варианта",
                    execution_hours=24,
                    fee_payer=FeePayer.BUYER,
                    counterparty_user_id=SELLER,
                )
            )
            await uow.deals.accept_deal(SELLER, deal.id, version=1, payout_address=SELLER_ADDRESSES[network])
            request = await uow.deals.start_payment(BUYER, deal.id)
            return deal.id, request


def usdt(network: Network) -> str:
    return asset_code("USDT", network)


@pytest.fixture
async def oc(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, chains: ChainsConfig
) -> AsyncIterator[OC]:
    clock = Clock()
    settings = settings.model_copy(update={"bot_username": "escrow_test_bot"})
    ctx = BotContext(settings=settings, chains=chains, sessionmaker=sessionmaker, kv=MemoryKV(), clock=clock)
    gateway = OnchainGateway(settings, chains, clock=clock)
    networks = (Network.TRON, Network.BSC, Network.TON, Network.SOL, Network.ETH)
    ctx.gateway = RoutingGateway(dict.fromkeys(networks, gateway))
    adapters = {n: FakeAdapter(n, chains.networks[n].deposit_address) for n in networks}
    async with sessionmaker() as s, s.begin():
        await make_user(s, BUYER, "alice")
        await make_user(s, SELLER, "bobby")
        await make_user(s, ADMIN, "admin", role=UserRole.ADMIN)
    yield OC(ctx, clock, adapters)
