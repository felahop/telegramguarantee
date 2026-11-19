from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import IdempotencyConflict, LedgerError
from app.domain.money import native_asset_code
from app.services import ledger as L
from app.services.ledger import AccountRef, Ledger
from tests.factories import make_deal_row, make_user

USDT = "USDT@tron"
WALLET = "THotWallet"


async def _deal(session: AsyncSession) -> int:
    await make_user(session, 1, "buyer")
    await make_user(session, 2, "seller")
    deal = await make_deal_row(session, buyer_id=1, seller_id=2)
    return deal.id


async def test_full_deal_flow_balances_to_zero(session: AsyncSession) -> None:
    """The worked example from ARCHITECTURE.md §11: 100 USDT, buyer pays the 2% fee,
    seller pays the 1.50 network fee, payment tag discount 0.0137."""
    deal_id = await _deal(session)
    ledger = Ledger(session)
    paid = 101_986_300  # 102.00 - 0.0137

    await ledger.post(
        idempotency_key=f"deposit:{deal_id}",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=paid),
        deal_id=deal_id,
    )
    assert await ledger.normal_balance(AccountRef.escrow(USDT, deal_id)) == paid
    assert await ledger.normal_balance(AccountRef.custody(USDT, WALLET)) == paid

    await ledger.post(
        idempotency_key=f"release:{deal_id}",
        kind="buyer_confirmed",
        postings=L.release_escrow(
            asset=USDT,
            deal_id=deal_id,
            to_payable=98_500_000,
            fee_revenue=1_986_300,
            network_fee_recovered=1_500_000,
        ),
        deal_id=deal_id,
    )
    await ledger.post(
        idempotency_key=f"payout:{deal_id}",
        kind="payout_settled",
        postings=L.payout_settled(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=98_500_000),
        deal_id=deal_id,
    )
    trx = native_asset_code("tron")
    await ledger.post(
        idempotency_key="gas-topup:1",
        kind="operator_funding",
        postings=L.operator_funding(asset=trx, wallet=WALLET, amount=10_000_000),
    )
    await ledger.post(
        idempotency_key=f"gas-spent:{deal_id}",
        kind="network_fee_spent",
        postings=L.network_fee_spent(native_asset=trx, wallet=WALLET, amount=345_000),
    )
    await session.commit()

    assert await ledger.normal_balance(AccountRef.escrow(USDT, deal_id)) == 0
    assert await ledger.normal_balance(AccountRef.payable(USDT, deal_id)) == 0
    assert await ledger.normal_balance(AccountRef.fee_revenue(USDT)) == 1_986_300
    assert await ledger.normal_balance(AccountRef.network_fee_recovered(USDT)) == 1_500_000
    # Custody now holds exactly the service's revenue.
    assert await ledger.normal_balance(AccountRef.custody(USDT, WALLET)) == 3_486_300
    assert await ledger.normal_balance(AccountRef.network_fee_expense(trx)) == 345_000
    assert await ledger.normal_balance(AccountRef.custody(trx, WALLET)) == 10_000_000 - 345_000
    assert await ledger.find_inconsistent_accounts() == []


async def test_unbalanced_postings_rejected_before_db(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    with pytest.raises(LedgerError, match="unbalanced"):
        await Ledger(session).post(
            idempotency_key="bad",
            kind="x",
            postings=[
                L.debit(AccountRef.custody(USDT, WALLET), 100),
                L.credit(AccountRef.escrow(USDT, deal_id), 99),
            ],
        )


async def test_assets_never_net_out(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    with pytest.raises(LedgerError, match="unbalanced"):
        await Ledger(session).post(
            idempotency_key="cross-asset",
            kind="x",
            postings=[
                L.debit(AccountRef.custody("USDT@tron", WALLET), 100),
                L.credit(AccountRef.escrow("USDT@bsc", deal_id), 100),
            ],
        )


async def test_database_rejects_unbalanced_transaction_at_commit(session: AsyncSession) -> None:
    """Even if application code is bypassed, the deferred trigger refuses an unbalanced commit."""
    deal_id = await _deal(session)
    ledger = Ledger(session)
    txn = await ledger.post(
        idempotency_key="ok",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    account_id = (
        await session.execute(text("SELECT id FROM ledger_accounts WHERE code='custody'"))
    ).scalar_one()
    await session.execute(
        text("INSERT INTO ledger_entries (transaction_id, account_id, asset, amount) VALUES (:t, :a, :s, 5)"),
        {"t": txn.id, "a": account_id, "s": USDT},
    )
    with pytest.raises(DBAPIError, match="unbalanced"):
        await session.commit()


async def test_entry_asset_must_match_account_asset(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    txn = await Ledger(session).post(
        idempotency_key="ok",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    account_id = (
        await session.execute(text("SELECT id FROM ledger_accounts WHERE code='custody'"))
    ).scalar_one()
    with pytest.raises(DBAPIError, match="fk_ledger_entries_account_asset"):
        await session.execute(
            text(
                "INSERT INTO ledger_entries (transaction_id, account_id, asset, amount) "
                "VALUES (:t, :a, 'USDT@bsc', 5)"
            ),
            {"t": txn.id, "a": account_id},
        )


async def test_idempotent_replay(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    ledger = Ledger(session)
    postings = L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100)
    first = await ledger.post(idempotency_key="dep", kind="deposit", postings=postings)
    second = await ledger.post(idempotency_key="dep", kind="deposit", postings=postings)
    assert first.id == second.id
    assert await ledger.normal_balance(AccountRef.escrow(USDT, deal_id)) == 100


async def test_reused_key_with_different_postings_conflicts(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    ledger = Ledger(session)
    await ledger.post(
        idempotency_key="dep",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    with pytest.raises(IdempotencyConflict):
        await ledger.post(
            idempotency_key="dep",
            kind="deposit",
            postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=200),
        )


async def test_cannot_release_more_than_escrowed(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    ledger = Ledger(session)
    await ledger.post(
        idempotency_key="dep",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    with pytest.raises(LedgerError, match="insufficient balance"):
        await ledger.post(
            idempotency_key="rel",
            kind="release",
            postings=L.release_escrow(asset=USDT, deal_id=deal_id, to_payable=101),
        )
    # the savepoint keeps the outer transaction usable after the domain error
    assert await ledger.normal_balance(AccountRef.escrow(USDT, deal_id)) == 100


async def test_custody_cannot_go_negative(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    ledger = Ledger(session)
    await ledger.post(
        idempotency_key="dep",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    await ledger.post(
        idempotency_key="rel",
        kind="release",
        postings=L.release_escrow(asset=USDT, deal_id=deal_id, to_payable=100),
    )
    # paying out from a different (empty) wallet must fail
    with pytest.raises(LedgerError, match="insufficient balance"):
        await ledger.post(
            idempotency_key="pay",
            kind="payout",
            postings=L.payout_settled(asset=USDT, wallet="TOtherWallet", deal_id=deal_id, amount=100),
        )


async def test_reverse_restores_balances_once(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    ledger = Ledger(session)
    await ledger.post(
        idempotency_key="dep",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    reversal = await ledger.reverse(original_key="dep", idempotency_key="dep:rev", reason="test")
    assert reversal.kind == "reversal:deposit"
    assert await ledger.normal_balance(AccountRef.escrow(USDT, deal_id)) == 0
    assert await ledger.normal_balance(AccountRef.custody(USDT, WALLET)) == 0
    with pytest.raises(LedgerError, match="already reversed"):
        await ledger.reverse(original_key="dep", idempotency_key="dep:rev2", reason="again")


async def test_ledger_is_append_only(session: AsyncSession) -> None:
    deal_id = await _deal(session)
    await Ledger(session).post(
        idempotency_key="dep",
        kind="deposit",
        postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
    )
    for sql in ("UPDATE ledger_entries SET amount = amount * 2", "DELETE FROM ledger_transactions"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin_nested():
                await session.execute(text(sql))


async def test_concurrent_double_release_only_one_wins(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two workers try to release the same escrow at once: row locks + CHECK let exactly one through."""
    async with sessionmaker() as s, s.begin():
        deal_id = await _deal(s)
        await Ledger(s).post(
            idempotency_key="dep",
            kind="deposit",
            postings=L.deposit_matched(asset=USDT, wallet=WALLET, deal_id=deal_id, amount=100),
        )

    async def release(key: str) -> str:
        async with sessionmaker() as s:
            try:
                async with s.begin():
                    await Ledger(s).post(
                        idempotency_key=key,
                        kind="release",
                        postings=L.release_escrow(asset=USDT, deal_id=deal_id, to_payable=100),
                    )
                    await asyncio.sleep(0.05)  # hold the lock while the other one waits
                return "ok"
            except LedgerError:
                return "rejected"

    results = sorted(await asyncio.gather(release("rel-a"), release("rel-b")))
    assert results == ["ok", "rejected"]
    async with sessionmaker() as s:
        assert await Ledger(s).normal_balance(AccountRef.escrow(USDT, deal_id)) == 0
        assert await Ledger(s).normal_balance(AccountRef.payable(USDT, deal_id)) == 100


def test_postings_reject_bad_amounts() -> None:
    acc = AccountRef.custody(USDT, WALLET)
    for bad in (0, -1, True, 1.5):
        with pytest.raises((LedgerError, TypeError)):
            L.debit(acc, bad)  # type: ignore[arg-type]
    with pytest.raises(LedgerError):
        L.release_escrow(asset=USDT, deal_id=1, to_payable=0)
