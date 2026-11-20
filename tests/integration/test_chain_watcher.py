"""ChainWatcher core with a scripted adapter: crediting only final, verified transfers; drops; matching."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.chains.base import ChainUnavailable
from app.db.models import ChainCursor, PaymentRequest, Payout
from app.domain.enums import (
    DealStatus,
    MatchMethod,
    MatchStatus,
    Network,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    TransferStatus,
)
from app.domain.errors import LimitExceeded
from app.providers.onchain.gateway import OnchainGateway
from app.services.ledger import AccountRef
from tests.integration.chain_world import OC, usdt
from tests.integration.deal_world import ADMIN, BUYER, SELLER

U = 10**6
TRON = Network.TRON


async def test_first_run_starts_near_the_final_head(oc: OC) -> None:
    oc.adapters[TRON].final = oc.adapters[TRON].latest = 5000
    await oc.tick()
    async with oc.ctx.sessionmaker() as s:
        cursor = await s.get(ChainCursor, (TRON, "deposits"))
        assert cursor is not None
        assert (cursor.cursor_final, cursor.head_final, cursor.active_endpoint) == ("5000", 5000, "fake-rpc")
    assert oc.adapters[TRON].scans == [(4000, 5000, True)]  # one span back, not the whole chain


async def test_seen_then_final_funds_the_deal(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()  # cursor at 100
    deal_id, request = await oc.awaiting()
    assert request.tag_reserved
    assert 102 * U - 99_900 <= request.expected_amount < 102 * U  # discount tag: never more than shown

    chain.send("tx1", request.expected_amount, at=105)
    chain.latest = 110  # in a block, not final yet
    result = await oc.tick()
    assert (result.seen, result.final) == (1, 0)
    [t] = await oc.transfers()
    assert (t.status, t.deal_id, t.position) == (TransferStatus.SEEN, deal_id, 105)
    assert "payment_seen" in await oc.notices(BUYER)
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT  # nothing credited yet
    assert await oc.balance(AccountRef.escrow(usdt(TRON), deal_id)) == 0

    chain.final = 110
    result = await oc.tick()
    assert result.final == 1
    [t] = await oc.transfers()
    assert (t.status, t.match_status, t.match_method) == (
        TransferStatus.FINAL,
        MatchStatus.MATCHED,
        MatchMethod.AMOUNT,
    )
    deal = await oc.deal(deal_id)
    assert deal.status is DealStatus.FUNDED
    assert await oc.balance(AccountRef.custody(usdt(TRON), chain.address)) == request.expected_amount

    # a replica (or a restart) scanning the same range again changes nothing
    async with oc.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.update(ChainCursor).values(cursor_final="100", cursor_seen="100"))
    await oc.tick()
    assert await oc.balance(AccountRef.custody(usdt(TRON), chain.address)) == request.expected_amount
    assert len(await oc.transfers()) == 1


async def test_dropped_transfer_is_never_credited(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    chain.send("tx-reorged", request.expected_amount, at=105)
    chain.latest = 106
    await oc.tick()
    del chain.transfers[("tx-reorged", 0)]  # the block was reorganized away
    chain.latest = chain.final = 120
    result = await oc.tick()
    assert result.dropped == 1
    [t] = await oc.transfers()
    assert (t.status, t.deal_id) == (TransferStatus.DROPPED, None)
    assert "payment_dropped" in await oc.notices(BUYER)
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT


async def test_second_source_disagreeing_blocks_crediting(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    chain.send("tx-fake", request.expected_amount, at=105)
    chain.disputed.add("tx-fake")
    chain.latest = chain.final = 110
    await oc.tick()
    [t] = await oc.transfers()
    assert (t.status, t.match_status) == (TransferStatus.NEEDS_REVIEW, MatchStatus.PENDING)
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    assert await oc.balance(AccountRef.custody(usdt(TRON), chain.address)) == 0
    assert "admin_payment_anomaly" in await oc.notices(ADMIN)


async def test_verification_outage_keeps_the_cursor(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    _, request = await oc.awaiting()
    chain.send("tx1", request.expected_amount, at=105)
    chain.latest = chain.final = 110
    chain.verify_down = True
    watcher = oc.watcher()
    with pytest.raises(ChainUnavailable):
        await watcher.tick()
    await watcher.record_error(ChainUnavailable("second source is down"))
    async with oc.ctx.sessionmaker() as s:
        cursor = await s.get(ChainCursor, (TRON, "deposits"))
        assert cursor is not None
        assert cursor.cursor_final == "100"
        assert "second source is down" in (cursor.last_error or "")
    chain.verify_down = False
    await oc.tick()
    assert (await oc.transfers())[0].status is TransferStatus.FINAL


async def test_unknown_amount_goes_to_unidentified(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    chain.send("tx-exchange", request.expected_amount - U, at=105)  # an exchange withheld 1 USDT
    chain.latest = chain.final = 110
    await oc.tick()
    [t] = await oc.transfers()
    assert (t.match_status, t.deal_id) == (MatchStatus.UNIDENTIFIED, None)
    assert await oc.balance(AccountRef.unidentified(usdt(TRON))) == request.expected_amount - U
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    assert "admin_payment_anomaly" in await oc.notices(ADMIN)


async def test_ton_comment_matches_partial_then_full_payment(oc: OC) -> None:
    chain = oc.adapters[Network.TON]
    await oc.tick(Network.TON)
    deal_id, request = await oc.awaiting(Network.TON)
    deal = await oc.deal(deal_id)
    assert request.memo == deal.code
    chain.send("ton-1", 60 * U, at=105, memo=f"  {deal.code.lower()} ")
    chain.latest = chain.final = 110
    await oc.tick(Network.TON)
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    assert "partial_payment" in await oc.notices(BUYER)
    assert "partial_payment" in await oc.notices(SELLER)
    chain.send("ton-2", request.expected_amount - 60 * U, at=115, memo=deal.code)
    chain.latest = chain.final = 120
    await oc.tick(Network.TON)
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED
    assert [t.match_method for t in await oc.transfers()] == [MatchMethod.MEMO, MatchMethod.MEMO]


async def test_solana_reference_matches_any_amount(oc: OC) -> None:
    chain = oc.adapters[Network.SOL]
    await oc.tick(Network.SOL)
    deal_id, request = await oc.awaiting(Network.SOL)
    assert request.sol_reference
    chain.send("sol-1", request.expected_amount + 3 * U, at=105, refs=("other", request.sol_reference))
    chain.latest = chain.final = 110
    await oc.tick(Network.SOL)
    deal = await oc.deal(deal_id)
    assert deal.status is DealStatus.FUNDED  # the 3 USDT excess goes back to the buyer
    async with oc.ctx.sessionmaker() as s:
        [refund] = list((await s.execute(sa.select(Payout))).scalars())
    assert (refund.kind, refund.status) == (PayoutKind.OVERPAY_REFUND, PayoutStatus.AWAITING_ADDRESS)


async def test_second_payment_for_a_funded_deal_is_not_auto_credited(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    chain.send("tx1", request.expected_amount, at=105)
    chain.send("tx2", request.expected_amount, at=106)  # paid twice (tag still reserved)
    chain.latest = chain.final = 110
    await oc.tick()
    first, second = await oc.transfers()
    assert (first.match_status, second.match_status) == (MatchStatus.MATCHED, MatchStatus.UNIDENTIFIED)
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED
    assert await oc.balance(AccountRef.unidentified(usdt(TRON))) == request.expected_amount


async def test_late_payment_for_an_expired_deal_is_refunded(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    oc.clock.advance(timedelta(minutes=31))
    async with oc.ctx.uow() as uow:
        assert await uow.deals.process_timer(deal_id)
    assert (await oc.deal(deal_id)).status is DealStatus.EXPIRED
    chain.send("tx-late", request.expected_amount, at=105)
    chain.latest = chain.final = 110
    await oc.tick()
    [t] = await oc.transfers()
    assert t.match_status is MatchStatus.LATE
    assert (await oc.deal(deal_id)).status is DealStatus.REFUND_PENDING
    assert "refund_address_needed" in await oc.notices(BUYER)


async def test_payment_window_stays_open_while_a_transfer_confirms(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    deal_id, request = await oc.awaiting()
    chain.send("tx1", request.expected_amount, at=105)
    chain.latest = 106
    await oc.tick()
    oc.clock.advance(timedelta(minutes=31))
    async with oc.ctx.uow() as uow:
        await uow.deals.process_timer(deal_id)
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT  # deferred, not expired
    chain.final = 106
    await oc.tick()
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED


async def test_a_replica_that_lost_the_race_writes_nothing(oc: OC) -> None:
    chain = oc.adapters[TRON]
    await oc.tick()
    _, request = await oc.awaiting()
    chain.send("tx1", request.expected_amount, at=105)
    chain.latest = chain.final = 110
    slow = oc.watcher()
    original_verify = chain.verify

    async def verify_then_race(transfer):  # type: ignore[no-untyped-def]
        chain.verify = original_verify  # type: ignore[method-assign]
        await oc.tick()  # another replica processes the same range meanwhile
        return await original_verify(transfer)

    chain.verify = verify_then_race  # type: ignore[method-assign]
    assert (await slow.tick()).skipped
    assert len(await oc.transfers()) == 1


async def test_tags_are_unique_per_address_and_amount(oc: OC) -> None:
    _, first = await oc.awaiting()
    gateway = OnchainGateway(oc.ctx.settings, oc.ctx.chains, clock=oc.clock, randbelow=lambda _: 0)
    oc.ctx.gateway = type(oc.ctx.gateway)({TRON: gateway})  # type: ignore[call-arg]
    _, second = await oc.awaiting()
    assert second.expected_amount != first.expected_amount or first.tag_amount != second.tag_amount
    with pytest.raises(LimitExceeded):
        await oc.awaiting()  # the only tag the fake RNG can draw is taken
    async with oc.ctx.sessionmaker() as s:
        statuses = list((await s.execute(sa.select(PaymentRequest.status))).scalars())
    assert statuses.count(PaymentRequestStatus.ACTIVE) == 2


# ── reconciliation of deposit addresses ──────────────────────────────────────


async def test_deposit_address_balance_is_reconciled(oc: OC) -> None:
    from app.services.reconciliation import Severity, balance_sources, reconcile  # noqa: PLC0415

    chain = oc.adapters[TRON]
    await oc.tick()
    _, request = await oc.awaiting()
    chain.send("tx1", request.expected_amount, at=105)
    chain.latest = chain.final = 110
    await oc.tick()

    async def run() -> list[tuple[str, Severity]]:
        report = await reconcile(
            oc.ctx.sessionmaker,
            oc.ctx.settings,
            oc.ctx.chains,
            sources=balance_sources(oc.ctx.chains, None, [chain]),  # type: ignore[list-item]
            now=oc.clock(),
        )
        assert [r.wallet for r in report.runs] == [chain.address]
        return [(f.check, f.severity) for f in report.findings if f.check in ("balance", "chain_watcher")]

    assert await run() == []
    chain.transfers.clear()  # the money left the deposit address outside the bot
    await run()
    oc.clock.advance(timedelta(minutes=15))
    assert ("balance", Severity.CRITICAL) in await run()


async def test_stalled_watcher_is_critical(oc: OC) -> None:
    from app.services.reconciliation import reconcile  # noqa: PLC0415

    await oc.tick()
    async with oc.ctx.sessionmaker() as s, s.begin():
        await s.execute(
            sa.update(ChainCursor).values(
                updated_at=sa.func.now() - timedelta(minutes=30), last_error="HTTP 429"
            )
        )
    report = await reconcile(oc.ctx.sessionmaker, oc.ctx.settings, oc.ctx.chains, sources=[], now=oc.clock())
    [finding] = [f for f in report.findings if f.check == "chain_watcher"]
    assert "tron" in finding.message
    assert "HTTP 429" in finding.message
