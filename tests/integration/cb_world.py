"""CryptoBot test world: settings, an in-memory Crypto Pay, users and deal helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.storage import MemoryKV
from app.config import ChainsConfig, Settings
from app.db.models import Deal, NotificationOutbox, Payout
from app.domain.enums import FeePayer, Network, PartyRole, UserRole
from app.providers.cryptobot.client import CryptoPayClient, Invoice
from app.providers.cryptobot.gateway import CryptoBotGateway
from app.services.deals import NewDeal
from app.services.gateways import RoutingGateway
from app.services.ledger import AccountRef, Ledger
from app.services.payments import ApplyResult, PaymentService
from tests.cryptobot_fake import TOKEN, FakeCryptoPay
from tests.factories import make_user
from tests.integration.deal_world import ADMIN, BUYER, SELLER, Clock

U = 10**6
USDT = "USDT@cryptobot"
HOOK_PATH = "hook-secret"


@dataclass
class CB:
    ctx: BotContext
    client: CryptoPayClient
    fake: FakeCryptoPay
    clock: Clock

    async def apply(self, invoice: dict[str, object]) -> ApplyResult:
        async with self.ctx.uow() as uow:
            return await PaymentService(
                uow.session, self.ctx.settings, self.ctx.chains, gateway=self.ctx.gateway, clock=self.clock
            ).apply_cryptobot_invoice(Invoice.model_validate(invoice))

    async def deal(self, deal_id: int) -> Deal:
        async with self.ctx.sessionmaker() as s:
            deal = await s.get(Deal, deal_id)
            assert deal is not None
            return deal

    async def balance(self, account: AccountRef) -> int:
        async with self.ctx.sessionmaker() as s:
            return await Ledger(s).normal_balance(account)

    async def payouts(self, deal_id: int) -> list[Payout]:
        async with self.ctx.sessionmaker() as s:
            return list((await s.execute(sa.select(Payout).where(Payout.deal_id == deal_id))).scalars())

    async def notices(self, user_id: int) -> list[str]:
        async with self.ctx.sessionmaker() as s:
            rows = await s.execute(
                sa.select(NotificationOutbox.kind).where(NotificationOutbox.user_id == user_id)
            )
            return list(rows.scalars())


@pytest.fixture
async def cb(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, chains: ChainsConfig
) -> AsyncIterator[CB]:
    clock = Clock()
    fake = FakeCryptoPay()
    settings = settings.model_copy(
        update={
            "cryptobot_api_token": SecretStr(TOKEN),
            "cryptobot_testnet": True,
            "bot_username": "escrow_test_bot",
            "bot_token": SecretStr("42:TEST"),
            "cryptobot_webhook_secret_path": SecretStr(HOOK_PATH),
        }
    )
    client = CryptoPayClient(settings.cryptobot_api_token, testnet=True, transport=fake.transport())
    ctx = BotContext(settings=settings, chains=chains, sessionmaker=sessionmaker, kv=MemoryKV(), clock=clock)
    ctx.gateway = RoutingGateway({Network.CRYPTOBOT: CryptoBotGateway(client, settings, chains, clock=clock)})
    async with sessionmaker() as s, s.begin():
        await make_user(s, BUYER, "alice")
        await make_user(s, SELLER, "bobby")
        await make_user(s, ADMIN, "admin", role=UserRole.ADMIN)
    yield CB(ctx, client, fake, clock)
    await client.close()


async def awaiting(cb: CB, amount: str = "100") -> tuple[int, int]:
    """A CryptoBot deal waiting for payment → (deal id, invoice id)."""
    async with cb.ctx.uow() as uow:
        deal = await uow.deals.create_deal(
            NewDeal(
                creator_id=BUYER,
                creator_role=PartyRole.BUYER,
                network=Network.CRYPTOBOT,
                amount=Decimal(amount),
                terms="Логотип для кофейни",
                execution_hours=24,
                fee_payer=FeePayer.BUYER,
                counterparty_user_id=SELLER,
            )
        )
        await uow.deals.accept_deal(SELLER, deal.id, version=1)
        request = await uow.deals.start_payment(BUYER, deal.id)
        return deal.id, int(request.external_id or 0)


async def confirmed_deal(cb: CB, amount: str = "100") -> int:
    deal_id, invoice_id = await awaiting(cb, amount)
    await cb.apply(cb.fake.pay(invoice_id))
    async with cb.ctx.uow() as uow:
        await uow.deals.seller_confirm(SELLER, deal_id)
        await uow.deals.buyer_confirm(BUYER, deal_id)
    return deal_id
