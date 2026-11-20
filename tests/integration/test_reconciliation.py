"""Reconciliation on a real database: balances against the in-memory CryptoBot, ledger invariants,
stuck work, housekeeping and the payout circuit breaker."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ChainsConfig, Settings
from app.db.models import (
    LedgerAccount,
    NotificationOutbox,
    PaymentRequest,
    Payout,
    PayoutPause,
    ReconciliationRun,
)
from app.domain.enums import (
    DealStatus,
    Network,
    OutboxStatus,
    PaymentRequestStatus,
    PayoutStatus,
    UserRole,
)
from app.domain.errors import InvalidTransition
from app.services import ledger as L
from app.services.admin import AdminService
from app.services.ledger import AccountRef, Ledger
from app.services.notifications import Notice
from app.services.payments import ApplyResult
from app.services.reconciliation import Reconciler, Report, Severity, balance_sources, reconcile
from app.worker.cryptobot import CryptoBotPayouts
from tests.integration.cb_world import CB, USDT, awaiting, confirmed_deal
from tests.integration.deal_world import ADMIN, BUYER, SELLER, SELLER_ADDRESSES, World

U = 10**6
STEP = timedelta(minutes=15)


async def run(cb: CB, *, sender_alive: bool = True) -> Report:
    if sender_alive:  # the outbox worker delivered everything queued so far
        async with cb.ctx.sessionmaker() as s, s.begin():
            await s.execute(
                sa.update(NotificationOutbox)
                .where(NotificationOutbox.status == OutboxStatus.PENDING)
                .values(status=OutboxStatus.SENT)
            )
    return await reconcile(
        cb.ctx.sessionmaker,
        cb.ctx.settings,
        cb.ctx.chains,
        sources=balance_sources(cb.ctx.chains, cb.client),
        now=cb.clock(),
    )


def checks(report: Report) -> dict[str, Severity]:
    return {f.check: f.severity for f in report.findings}


async def admin_alerts(cb: CB) -> list[dict[str, object]]:
    async with cb.ctx.sessionmaker() as s:
        rows = await s.execute(
            sa.select(NotificationOutbox.payload).where(
                NotificationOutbox.kind == Notice.ADMIN_RECONCILIATION.value,
                NotificationOutbox.user_id == ADMIN,
            )
        )
        return list(rows.scalars())


async def settle_all(cb: CB) -> None:
    while await CryptoBotPayouts(cb.ctx, cb.client).run_once():
        pass


# ── balances ─────────────────────────────────────────────────────────────────


async def test_balanced_after_a_full_deal(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    await settle_all(cb)
    assert (await cb.deal(deal_id)).status is DealStatus.COMPLETED
    report = await run(cb)
    [balance] = report.runs
    assert (balance.wallet, balance.asset, balance.ok, balance.diff) == ("cryptobot", USDT, True, 0)
    assert balance.chain_balance == balance.ledger_balance == 2 * U  # the service fee stays with us
    assert report.findings == []
    assert await admin_alerts(cb) == []


async def test_a_payment_between_the_two_reads_is_not_an_alert(cb: CB) -> None:
    _, invoice_id = await awaiting(cb)
    paid = cb.fake.pay(invoice_id)  # paid at CryptoBot, the webhook has not arrived yet
    first = await run(cb)
    assert first.runs[0].diff == 102 * U
    assert first.findings == []  # one run proves nothing
    assert await cb.apply(paid) is ApplyResult.FUNDED
    cb.clock.advance(STEP)
    second = await run(cb)
    assert second.runs[0].ok
    assert second.findings == []


async def test_persistent_deficit_alerts_and_stops_payouts(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.balance_adjustment = Decimal("-5")  # money left the app balance outside the bot
    assert (await run(cb)).findings == []
    cb.clock.advance(timedelta(minutes=3))
    assert (await run(cb)).findings == []  # too close to the previous run to confirm anything
    cb.clock.advance(STEP)
    report = await run(cb)
    [finding] = [f for f in report.findings if f.check == "balance"]
    assert finding.severity is Severity.CRITICAL
    assert "не хватает 5.00" in finding.message
    assert report.paused == ["cryptobot"]
    [alert] = await admin_alerts(cb)
    assert alert["severity"] == "critical"

    # the circuit breaker holds the seller's payout
    assert not await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert cb.fake.calls_to("transfer") == []
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.QUEUED
    assert "payout_queue" not in checks(report)  # held on purpose, not stuck

    cb.clock.advance(STEP)
    await run(cb)
    assert len(await admin_alerts(cb)) == 1  # the same incident is reported once a day

    # the operator finds the cause, tops the balance up and an admin resumes payouts
    cb.fake.balance_adjustment = Decimal(0)
    async with cb.ctx.uow() as uow:
        admin = AdminService(uow.session, cb.ctx.settings, cb.ctx.chains, clock=cb.clock)
        await admin.resume_payouts(ADMIN, "cryptobot")
    with pytest.raises(InvalidTransition):
        async with cb.ctx.uow() as uow:
            await AdminService(uow.session, cb.ctx.settings, cb.ctx.chains).resume_payouts(ADMIN, "cryptobot")
    await settle_all(cb)
    assert (await cb.deal(deal_id)).status is DealStatus.COMPLETED
    cb.clock.advance(STEP)
    assert (await run(cb)).runs[0].ok


async def test_surplus_is_a_warning_and_never_pauses(cb: CB) -> None:
    cb.fake.balance_adjustment = Decimal("50")  # the operator topped up and did not record it
    await run(cb)
    cb.clock.advance(STEP)
    report = await run(cb)
    assert checks(report)["balance"] is Severity.WARNING
    assert "record-funding" in report.findings[0].message
    assert report.paused == []
    async with cb.ctx.sessionmaker() as s:
        assert await s.get(PayoutPause, "cryptobot") is None


async def test_deficit_of_a_payout_with_unknown_outcome_is_in_flight(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.timeout_after_transfer = True  # executed, but the response is lost
    cb.fake.errors["getTransfers"] += [httpx.ConnectError("down")] * 5  # and the lookup fails
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.FAILED_RETRYABLE
    assert (payout.last_error or "").startswith("lookup:")
    await run(cb)
    cb.clock.advance(STEP)
    report = await run(cb)
    [balance] = report.runs
    assert balance.diff == -100 * U
    assert balance.in_flight == 100 * U
    assert "balance" not in checks(report)  # explained: the retry resolves it by spend_id
    assert report.paused == []


async def test_provider_down_still_checks_the_ledger(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id))
    await _tamper_escrow(cb, deal_id)
    cb.fake.errors["getBalance"] += [httpx.ConnectError("down")] * 5
    report = await run(cb)
    assert report.source_errors == ["cryptobot"]
    assert report.runs == []
    assert "ledger_cache" in checks(report)


async def _tamper_escrow(cb: CB, deal_id: int) -> None:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.text("SET LOCAL session_replication_role = replica"))  # bypass the guards
        await s.execute(
            sa.update(LedgerAccount)
            .where(LedgerAccount.deal_id == deal_id, LedgerAccount.code == "escrow")
            .values(balance=LedgerAccount.balance - 1)
        )


async def test_only_one_run_at_a_time(cb: CB) -> None:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.select(sa.func.pg_advisory_xact_lock(0x5EC0_0001)))
        report = await run(cb)
    assert report.skipped
    async with cb.ctx.sessionmaker() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(ReconciliationRun))).scalar_one() == 0


# ── ledger invariants ────────────────────────────────────────────────────────


async def test_tampered_balance_cache_is_critical(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id))
    await _tamper_escrow(cb, deal_id)
    report = await run(cb)
    found = checks(report)
    assert found["ledger_cache"] is Severity.CRITICAL
    assert found["ledger_trial_balance"] is Severity.CRITICAL


async def test_money_left_on_a_closed_deal_and_payable_without_payout(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    await settle_all(cb)
    async with cb.ctx.sessionmaker() as s, s.begin():
        ledger = Ledger(s)
        await ledger.post(
            idempotency_key="test:stray-escrow",
            kind="adjustment",
            postings=[
                L.debit(AccountRef.adjustments(USDT), U),
                L.credit(AccountRef.escrow(USDT, deal_id), U),
            ],
        )
        await ledger.post(
            idempotency_key="test:stray-payable",
            kind="adjustment",
            postings=[
                L.debit(AccountRef.adjustments(USDT), U),
                L.credit(AccountRef.payable(USDT, deal_id), U),
            ],
        )
    report = await run(cb)
    closed = [f for f in report.findings if f.check == "closed_deal_money"]
    assert {f.deal_id for f in closed} == {deal_id}
    assert len(closed) == 2  # escrow and payable
    [mismatch] = [f for f in report.findings if f.check == "payable_mismatch"]
    assert "к выплате по ledger 1.00, а в незавершённых выплатах 0.00" in mismatch.message


async def test_payable_matches_open_payouts_during_a_normal_deal(cb: CB) -> None:
    await confirmed_deal(cb)  # payable 100, payout queued 100
    report = await run(cb)
    assert "payable_mismatch" not in checks(report)
    assert "closed_deal_money" not in checks(report)


async def test_provider_fees_uncovered_by_revenue_are_a_solvency_warning(cb: CB) -> None:
    _, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id, fee="3.06"))  # escrow 102, only 98.94 arrived
    report = await run(cb)
    assert report.runs[0].ok  # the wallet matches the ledger…
    assert checks(report)["solvency"] is Severity.WARNING  # …but users are owed more than it holds
    assert "не хватает 3.06" in report.findings[0].message


# ── stuck work ───────────────────────────────────────────────────────────────


async def test_payout_queue_and_timers_not_moving(cb: CB) -> None:
    await confirmed_deal(cb)  # a payout is due now
    await awaiting(cb)  # a payment timer is due in 30 min
    cb.clock.advance(timedelta(minutes=45))  # the worker was down
    found = checks(await run(cb, sender_alive=False))
    assert found["payout_queue"] is Severity.CRITICAL
    assert found["timers"] is Severity.CRITICAL
    assert found["outbox"] is Severity.WARNING  # nobody sent the deal notices either


async def test_failed_payout_and_long_approval(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.errors["transfer"].append("METHOD_DISABLED")
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    big_deal = await _large_approval(cb)
    report = await run(cb)
    [failed] = [f for f in report.findings if f.check == "payout_failed_final"]
    assert (failed.severity, failed.deal_id) == (Severity.CRITICAL, deal_id)
    assert "METHOD_DISABLED" in failed.message
    assert "payout_awaiting_approval" not in checks(report)  # within the SLA
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(
            sa.update(Payout)
            .where(Payout.deal_id == big_deal)
            .values(created_at=sa.func.now() - timedelta(hours=5))
        )
    report = await run(cb)
    assert checks(report)["payout_awaiting_approval"] is Severity.WARNING


async def _large_approval(cb: CB) -> int:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.text("UPDATE users SET deals_completed = 5"))
    deal_id = await confirmed_deal(cb, amount="2500")
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.AWAITING_APPROVAL
    return deal_id


async def test_old_unidentified_payments(cb: CB) -> None:
    _, invoice_id = await awaiting(cb)
    assert await cb.apply(cb.fake.pay(invoice_id, amount="50")) is ApplyResult.ANOMALY
    assert "unidentified" not in checks(await run(cb))
    cb.clock.advance(timedelta(hours=25))
    report = await run(cb)
    assert checks(report)["unidentified"] is Severity.WARNING
    [finding] = [f for f in report.findings if f.check == "unidentified"]
    assert "1 шт., старейший — 25 ч" in finding.message


async def test_alert_button_leads_to_the_problem(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.errors["transfer"].append("METHOD_DISABLED")
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    await run(cb)
    [alert] = [a for a in await admin_alerts(cb) if a["check"] == "payout_failed_final"]
    [payout] = await cb.payouts(deal_id)
    assert alert["payout_id"] == payout.id


# ── housekeeping (world: on-chain deals with a fake gateway) ─────────────────


@pytest.fixture
async def world(session: AsyncSession, settings: Settings, chains: ChainsConfig) -> World:
    w = World(session, settings, chains)
    await w.setup_users()
    return w


async def test_refund_address_reminders_once_a_day(world: World) -> None:
    deal = await world.funded_deal(network=Network.TRON)
    await world.svc.request_cancel(SELLER, deal.id)
    await world.svc.request_cancel(BUYER, deal.id)
    [refund] = await world.payouts(deal)
    assert refund.status is PayoutStatus.AWAITING_ADDRESS

    async def remind() -> int:
        """Run reconciliation, return how many refund-address notices the buyer has in total."""
        await Reconciler(world.session, world.settings, world.chains, now=world.clock()).run([])
        return await _reminders(world)

    assert await remind() == 1  # the notice sent at the refund itself
    await world.session.execute(
        sa.update(Payout).where(Payout.id == refund.id).values(created_at=sa.func.now() - timedelta(hours=25))
    )
    assert await remind() == 2
    assert await remind() == 2  # not again the same day
    await world.session.execute(
        sa.update(Payout).where(Payout.id == refund.id).values(created_at=sa.func.now() - timedelta(hours=49))
    )
    assert await remind() == 3
    await world.session.execute(
        sa.update(Payout).where(Payout.id == refund.id).values(created_at=sa.func.now() - timedelta(hours=73))
    )
    report = await Reconciler(world.session, world.settings, world.chains, now=world.clock()).run([])
    assert {f.check: f.severity for f in report.findings}["payout_awaiting_address"] is Severity.WARNING


async def _reminders(world: World) -> int:
    rows = await world.session.execute(
        sa.select(sa.func.count())
        .select_from(NotificationOutbox)
        .where(
            NotificationOutbox.user_id == BUYER, NotificationOutbox.kind == Notice.REFUND_ADDRESS_NEEDED.value
        )
    )
    return int(rows.scalar_one())


async def test_unique_tags_are_released_after_the_cooldown(world: World) -> None:
    deal = await world.funded_deal(network=Network.TRON)
    [request] = list(
        (
            await world.session.execute(sa.select(PaymentRequest).where(PaymentRequest.deal_id == deal.id))
        ).scalars()
    )
    assert request.status is PaymentRequestStatus.PAID
    assert request.tag_reserved
    assert request.tag_release_at is not None
    reconciler = Reconciler(
        world.session, world.settings, world.chains, now=request.tag_release_at - timedelta(minutes=1)
    )
    assert (await reconciler.run([])).tags_released == 0
    reconciler = Reconciler(world.session, world.settings, world.chains, now=request.tag_release_at)
    assert (await reconciler.run([])).tags_released == 1
    await world.session.refresh(request)
    assert not request.tag_reserved


async def test_active_requests_keep_their_tag(world: World) -> None:
    deal = await world.new_deal(network=Network.TRON)
    await world.svc.accept_deal(
        SELLER, deal.id, version=deal.version, payout_address=SELLER_ADDRESSES[Network.TRON]
    )
    request = await world.svc.start_payment(BUYER, deal.id)
    assert request.tag_reserved
    request.tag_release_at = world.clock() - timedelta(days=1)  # even with a stale release time
    report = await Reconciler(world.session, world.settings, world.chains, now=world.clock()).run([])
    assert report.tags_released == 0


async def test_arbitrators_get_alerts_banned_staff_does_not(cb: CB) -> None:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(
            sa.text("UPDATE users SET role = :r WHERE id = :i").bindparams(
                r=UserRole.ARBITRATOR.value, i=SELLER
            )
        )
        await s.execute(sa.text("UPDATE users SET is_banned = true WHERE id = :i").bindparams(i=ADMIN))
    await confirmed_deal(cb)
    cb.clock.advance(timedelta(minutes=45))
    await run(cb)
    async with cb.ctx.sessionmaker() as s:
        users = set(
            (
                await s.execute(
                    sa.select(NotificationOutbox.user_id).where(
                        NotificationOutbox.kind == Notice.ADMIN_RECONCILIATION.value
                    )
                )
            ).scalars()
        )
    assert users == {SELLER}
