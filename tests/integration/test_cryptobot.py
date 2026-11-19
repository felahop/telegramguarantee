"""CryptoBot end to end: invoices, webhook, polling, provider sync, payouts — against a fake API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.context import BotContext
from app.bot.factory import build_bot, build_dispatcher
from app.bot.storage import MemoryKV
from app.config import ChainsConfig, Settings
from app.db.models import Deal, IncomingTransfer, NotificationOutbox, PaymentRequest, Payout
from app.domain.enums import (
    DealStatus,
    FeePayer,
    MatchStatus,
    Network,
    PartyRole,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    UserRole,
)
from app.domain.errors import ProviderUnavailable
from app.providers.cryptobot.client import CryptoPayClient, CryptoPayError, Invoice, verify_signature
from app.providers.cryptobot.gateway import CryptoBotGateway
from app.services.deals import NewDeal
from app.services.gateways import RoutingGateway
from app.services.ledger import AccountRef, Ledger
from app.services.payments import ApplyResult, PaymentService
from app.web.__main__ import build_app
from app.web.cryptopay import PATH_PREFIX
from app.worker.cryptobot import CryptoBotPayouts, poll_invoices
from app.worker.timers import run_due_timers
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


# ── invoices ─────────────────────────────────────────────────────────────────


async def test_invoice_is_created_with_our_payload(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    [params] = cb.fake.calls_to("createInvoice")
    async with cb.ctx.sessionmaker() as s:
        request = (await s.execute(sa.select(PaymentRequest))).scalar_one()
    assert params["asset"] == "USDT"
    assert params["amount"] == "102"  # 100 + 2 % fee paid by the buyer
    assert params["payload"] == f"pr:{request.id}:d:{deal_id}"
    assert params["expires_in"] == 1800
    assert params["allow_anonymous"] is False
    assert request.pay_url == f"https://t.me/CryptoTestnetBot?start=IV{invoice_id}"
    assert request.expected_amount == 102 * U


async def test_provider_outage_is_reported_and_rolled_back(cb: CB) -> None:
    cb.fake.errors["createInvoice"].append(httpx.ConnectError("down"))
    cb.fake.errors["createInvoice"].append(httpx.ConnectError("still down"))
    with pytest.raises(ProviderUnavailable):
        await awaiting(cb)
    async with cb.ctx.sessionmaker() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(PaymentRequest))).scalar_one() == 0


async def test_paid_invoice_funds_the_deal_once(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    invoice = cb.fake.pay(invoice_id)
    assert await cb.apply(invoice) is ApplyResult.FUNDED
    assert await cb.apply(invoice) is ApplyResult.DUPLICATE  # webhook re-delivered / poller
    assert (await cb.deal(deal_id)).status is DealStatus.FUNDED
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 102 * U
    assert await cb.balance(AccountRef.escrow(USDT, deal_id)) == 102 * U
    async with cb.ctx.sessionmaker() as s:
        [transfer] = list((await s.execute(sa.select(IncomingTransfer))).scalars())
        assert (transfer.match_status, transfer.deal_id) == (MatchStatus.MATCHED, deal_id)
    assert "deal_funded" in await cb.notices(SELLER)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"amount": "101"}, "amount_mismatch"),
        ({"asset": "TON"}, "asset"),
    ],
)
async def test_mismatching_payment_is_not_credited(cb: CB, change: dict[str, str], problem: str) -> None:
    deal_id, invoice_id = await awaiting(cb)
    invoice = cb.fake.pay(invoice_id, **change)
    assert await cb.apply(invoice) is ApplyResult.ANOMALY
    assert (await cb.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    assert await cb.balance(AccountRef.escrow(USDT, deal_id)) == 0
    if problem == "amount_mismatch":
        assert await cb.balance(AccountRef.unidentified(USDT)) == 101 * U
    assert "admin_payment_anomaly" in await cb.notices(ADMIN)


async def test_foreign_payload_is_not_credited(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    invoice = cb.fake.pay(invoice_id)
    invoice["payload"] = "pr:999:d:999"
    assert await cb.apply(invoice) is ApplyResult.ANOMALY
    assert (await cb.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT


async def test_unknown_invoice_goes_to_unidentified(cb: CB) -> None:
    await awaiting(cb)
    invoice = {"invoice_id": 1, "status": "paid", "asset": "USDT", "amount": "50", "payload": "x"}
    assert await cb.apply(invoice) is ApplyResult.ANOMALY
    assert await cb.balance(AccountRef.unidentified(USDT)) == 50 * U


async def test_provider_fee_is_an_expense(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id, fee="1.02"))
    assert await cb.balance(AccountRef.escrow(USDT, deal_id)) == 102 * U  # the buyer paid in full
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 102 * U - 1_020_000  # what arrived
    assert await cb.balance(AccountRef.provider_fee_expense(USDT)) == 1_020_000


async def test_cancel_deletes_the_invoice(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    async with cb.ctx.uow() as uow:
        await uow.deals.cancel(SELLER, deal_id)
    assert cb.fake.calls_to("deleteInvoice") == [{"invoice_id": invoice_id}]
    assert cb.fake.invoices[invoice_id]["status"] == "deleted"


async def test_payment_after_cancel_is_refunded(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    cb.fake.errors["deleteInvoice"].append("INVOICE_NOT_FOUND")  # deletion failed: the invoice stays payable
    async with cb.ctx.uow() as uow:
        await uow.deals.cancel(BUYER, deal_id)
    assert await cb.apply(cb.fake.pay(invoice_id)) is ApplyResult.LATE
    deal = await cb.deal(deal_id)
    assert deal.status is DealStatus.REFUND_PENDING
    [refund] = await cb.payouts(deal_id)
    assert (refund.kind, refund.recipient_user_id, refund.amount) == (PayoutKind.BUYER_REFUND, BUYER, 102 * U)


async def test_poller_applies_a_missed_webhook(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    cb.fake.pay(invoice_id)
    assert await poll_invoices(cb.ctx, cb.client) == 1
    assert (await cb.deal(deal_id)).status is DealStatus.FUNDED
    assert await poll_invoices(cb.ctx, cb.client) == 0  # request is no longer active


async def test_expiry_asks_the_provider_first(cb: CB) -> None:
    """A paid invoice whose webhook was lost must not be refunded as 'expired'."""
    deal_id, invoice_id = await awaiting(cb)
    cb.fake.pay(invoice_id)
    cb.clock.advance(cb.ctx.settings.payment_ttl)
    assert await run_due_timers(cb.ctx) == 1
    assert (await cb.deal(deal_id)).status is DealStatus.FUNDED


async def test_unpaid_invoice_expires(cb: CB) -> None:
    deal_id, _ = await awaiting(cb)
    cb.clock.advance(cb.ctx.settings.payment_ttl)
    await run_due_timers(cb.ctx)
    deal = await cb.deal(deal_id)
    assert deal.status is DealStatus.EXPIRED
    async with cb.ctx.sessionmaker() as s:
        request = (await s.execute(sa.select(PaymentRequest))).scalar_one()
        assert request.status is PaymentRequestStatus.EXPIRED


# ── webhook endpoint ─────────────────────────────────────────────────────────


async def _client(cb: CB) -> TestClient:  # type: ignore[type-arg]
    bot = build_bot(cb.ctx.settings)
    app = build_app(cb.ctx.settings, bot, build_dispatcher(cb.ctx, MemoryStorage()), cb.ctx)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_webhook_end_to_end(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    body, signature = cb.fake.webhook(cb.fake.pay(invoice_id))
    client = await _client(cb)
    try:
        url = PATH_PREFIX + HOOK_PATH
        forged = await client.post(
            url, data=body.replace(b'"102"', b'"1"'), headers={"crypto-pay-api-signature": signature}
        )
        assert forged.status == 401
        assert (await cb.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
        unsigned = await client.post(url, data=body)
        assert unsigned.status == 401
        for _ in range(2):  # duplicate delivery
            ok = await client.post(url, data=body, headers={"crypto-pay-api-signature": signature})
            assert ok.status == 200
        assert (
            await client.post(
                PATH_PREFIX + "wrong", data=body, headers={"crypto-pay-api-signature": signature}
            )
        ).status == 404
    finally:
        await client.close()
    assert (await cb.deal(deal_id)).status is DealStatus.FUNDED
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 102 * U


def test_signature_verification() -> None:
    fake = FakeCryptoPay()
    body, signature = fake.webhook({"invoice_id": 1, "status": "paid", "amount": "1"})
    assert verify_signature(TOKEN, body, signature)
    assert verify_signature(TOKEN, body, signature.upper())
    assert not verify_signature(TOKEN, body + b" ", signature)
    assert not verify_signature("other-token", body, signature)
    assert not verify_signature(TOKEN, body, "")


# ── payouts ──────────────────────────────────────────────────────────────────


async def confirmed_deal(cb: CB, amount: str = "100") -> int:
    deal_id, invoice_id = await awaiting(cb, amount)
    await cb.apply(cb.fake.pay(invoice_id))
    async with cb.ctx.uow() as uow:
        await uow.deals.seller_confirm(SELLER, deal_id)
        await uow.deals.buyer_confirm(BUYER, deal_id)
    return deal_id


async def test_payout_completes_the_deal(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.QUEUED
    assert await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    [params] = cb.fake.calls_to("transfer")
    assert (params["user_id"], params["amount"], params["spend_id"]) == (
        SELLER,
        "100",
        payout.idempotency_key,
    )
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.CONFIRMED
    assert (await cb.deal(deal_id)).status is DealStatus.COMPLETED
    assert await cb.balance(AccountRef.payable(USDT, deal_id)) == 0
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 2 * U  # only the service fee is left
    assert await cb.balance(AccountRef.fee_revenue(USDT)) == 2 * U
    assert not await CryptoBotPayouts(cb.ctx, cb.client).run_once()  # nothing left to pay


async def test_lost_response_is_resolved_without_paying_twice(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.timeout_after_transfer = True  # executed, but our side never saw the answer
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert len(cb.fake.transfers) == 1
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.CONFIRMED
    assert len(cb.fake.calls_to("transfer")) == 1
    assert len(cb.fake.calls_to("getTransfers")) == 1


async def test_spend_id_reuse_is_resolved_by_lookup(cb: CB) -> None:
    """Crash after a successful transfer but before our commit: the retry gets 'already used'."""
    deal_id = await confirmed_deal(cb)
    [payout] = await cb.payouts(deal_id)
    cb.fake.transfers[payout.idempotency_key] = {
        "transfer_id": 1,
        "spend_id": payout.idempotency_key,
        "user_id": SELLER,
        "asset": "USDT",
        "amount": "100",
        "status": "completed",
    }
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    [payout] = await cb.payouts(deal_id)
    assert (payout.status, payout.external_id) == (PayoutStatus.CONFIRMED, "1")
    assert (await cb.deal(deal_id)).status is DealStatus.COMPLETED


async def test_recipient_without_cryptobot_is_asked_to_start_it(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.errors["transfer"].append("USER_NOT_FOUND")
    executor = CryptoBotPayouts(cb.ctx, cb.client)
    await executor.run_once()
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.FAILED_RETRYABLE
    assert payout.next_attempt_at == cb.clock.now + timedelta(minutes=30)
    assert "payout_action_needed" in await cb.notices(SELLER)
    assert "admin_payout_failed" in await cb.notices(ADMIN)
    assert not await executor.run_once()  # waits for the retry time
    cb.clock.advance(timedelta(minutes=30))
    await executor.run_once()
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.CONFIRMED


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("INSUFFICIENT_FUNDS", PayoutStatus.FAILED_RETRYABLE),
        ("AMOUNT_TOO_SMALL", PayoutStatus.FAILED_FINAL),
        ("METHOD_DISABLED", PayoutStatus.FAILED_FINAL),  # transfers not enabled in the app settings
    ],
)
async def test_payout_errors(cb: CB, error: str, status: PayoutStatus) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.errors["transfer"].append(error)
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    [payout] = await cb.payouts(deal_id)
    assert payout.status is status
    assert (await cb.deal(deal_id)).status is DealStatus.PAYOUT_PENDING
    assert await cb.balance(AccountRef.payable(USDT, deal_id)) == 100 * U  # nothing left our books


async def test_large_payout_waits_for_admin(cb: CB) -> None:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.text("UPDATE users SET deals_completed = 5"))
    deal_id = await confirmed_deal(cb, amount="2500")
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.AWAITING_APPROVAL
    assert not await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert cb.fake.calls_to("transfer") == []


# ── client ───────────────────────────────────────────────────────────────────


async def test_reads_are_retried_writes_are_not() -> None:
    attempts = {"getInvoices": 0, "transfer": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        attempts[method] += 1
        if method == "getInvoices" and attempts[method] < 3:
            return httpx.Response(502)
        if method == "transfer":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"ok": True, "result": {"items": []}})

    client = CryptoPayClient(SecretStr(TOKEN), testnet=True, transport=httpx.MockTransport(handler))
    try:
        assert await client.get_invoices([1]) == []
        assert attempts["getInvoices"] == 3
        with pytest.raises(httpx.ReadTimeout):
            await client.transfer(user_id=1, asset="USDT", amount=Decimal(1), spend_id="s", comment="c")
        assert attempts["transfer"] == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("SPEND_ID_ALREADY_USED", "spend_id_used"),
        ("INSUFFICIENT_FUNDS", "insufficient_funds"),
        ("USER_NOT_FOUND", "recipient"),
        ("AMOUNT_TOO_SMALL", "amount"),
        ("METHOD_DISABLED", "disabled"),
        ("SOMETHING_NEW", "other"),
    ],
)
def test_error_classification(name: str, kind: str) -> None:
    assert CryptoPayError(name, 400).kind == kind
