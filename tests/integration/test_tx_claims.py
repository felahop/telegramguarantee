"""The buyer's "here is my transaction hash" fallback: never a shortcut around the matching rules."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.models import ChainCursor, TxClaim
from app.domain.enums import DealStatus, MatchStatus, Network, TxClaimStatus
from app.domain.errors import Forbidden, LimitExceeded, ValidationFailed
from app.services.tx_claims import normalize_hash, process_claims, submit_claim
from tests.integration.chain_world import OC
from tests.integration.deal_world import ADMIN, BUYER, SELLER

TRON = Network.TRON
U = 10**6
H = "ab" * 32


async def claim(oc: OC, deal_id: int, tx: str, user: int = BUYER) -> TxClaim:
    async with oc.ctx.uow() as uow:
        return await submit_claim(uow.session, user_id=user, deal_id=deal_id, tx_hash=tx, now=oc.clock())


async def process(oc: OC) -> list[TxClaim]:
    watchers = {TRON: oc.watcher()}
    await process_claims(oc.ctx.sessionmaker, oc.adapters, watchers, clock=oc.clock)  # type: ignore[arg-type]
    async with oc.ctx.sessionmaker() as s:
        return list((await s.execute(sa.select(TxClaim).order_by(TxClaim.id))).scalars())


async def missed_by_the_watcher(oc: OC, tx: str, amount: int) -> None:
    """A final transfer the scan never returned (an RPC gap): below the final cursor already."""
    chain = oc.adapters[TRON]
    chain.latest = chain.final = 200
    await oc.tick()  # the cursor passes 200
    chain.send(tx, amount, at=150)


async def test_a_missed_payment_is_credited_through_the_normal_rules(oc: OC) -> None:
    await oc.tick()
    deal_id, request = await oc.awaiting()
    await missed_by_the_watcher(oc, H, request.expected_amount)
    await claim(oc, deal_id, H.upper())  # case does not matter
    [c] = await process(oc)
    assert (c.status, c.result) == (TxClaimStatus.AUTO_MATCHED, "credited")
    assert (await oc.deal(deal_id)).status is DealStatus.FUNDED
    assert "tx_claim_result" in await oc.notices(BUYER)


async def test_a_different_amount_goes_to_the_admins_not_to_the_deal(oc: OC) -> None:
    await oc.tick()
    deal_id, request = await oc.awaiting()
    await missed_by_the_watcher(oc, H, request.expected_amount - U)
    await claim(oc, deal_id, H)
    [c] = await process(oc)
    assert (c.status, c.result) == (TxClaimStatus.PENDING_REVIEW, "needs_admin")
    assert (await oc.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT
    [t] = await oc.transfers()
    assert (c.incoming_transfer_id, t.match_status) == (t.id, MatchStatus.UNIDENTIFIED)
    assert "admin_payment_anomaly" in await oc.notices(ADMIN)


async def test_someone_elses_payment_cannot_be_claimed(oc: OC) -> None:
    await oc.tick()
    first_deal, first_request = await oc.awaiting()
    second_deal, _ = await oc.awaiting(amount="100")
    chain = oc.adapters[TRON]
    chain.send(H, first_request.expected_amount, at=105)
    chain.latest = chain.final = 110
    await oc.tick()  # credited to the first deal by its tag
    assert (await oc.deal(first_deal)).status is DealStatus.FUNDED
    await claim(oc, second_deal, H)  # the second buyer claims the first buyer's transaction
    [c] = await process(oc)
    assert (c.status, c.result) == (TxClaimStatus.REJECTED, "belongs_to_another_deal")
    assert (await oc.deal(second_deal)).status is DealStatus.AWAITING_PAYMENT
    assert "admin_payment_anomaly" in await oc.notices(ADMIN)


async def test_not_found_and_not_final_yet(oc: OC) -> None:
    await oc.tick()
    deal_id, request = await oc.awaiting()
    await claim(oc, deal_id, "cd" * 32)
    [c] = await process(oc)
    assert (c.status, c.result) == (TxClaimStatus.REJECTED, "no_usdt_to_us")
    chain = oc.adapters[TRON]
    chain.send("ef" * 32, request.expected_amount, at=chain.final + 5)
    chain.latest = chain.final + 10
    await claim(oc, deal_id, "ef" * 32)
    _, pending = await process(oc)
    assert pending.status is TxClaimStatus.SUBMITTED  # in a block, not final: looked at again later


async def test_validation_and_limits(oc: OC) -> None:
    await oc.tick()
    deal_id, _ = await oc.awaiting()
    with pytest.raises(ValidationFailed):
        await claim(oc, deal_id, "not-a-hash")
    with pytest.raises(Forbidden):
        await claim(oc, deal_id, H, user=SELLER)
    assert normalize_hash(Network.BSC, "AB" * 32) == "0x" + "ab" * 32
    again = await claim(oc, deal_id, H)
    assert (await claim(oc, deal_id, H)).id == again.id  # the same claim, not a new one
    for i in range(4):
        await claim(oc, deal_id, f"{i:02x}" * 32)
    with pytest.raises(LimitExceeded):
        await claim(oc, deal_id, "99" * 32)


async def test_cursor_untouched_by_claims(oc: OC) -> None:
    await oc.tick()
    deal_id, request = await oc.awaiting()
    await missed_by_the_watcher(oc, H, request.expected_amount)
    await claim(oc, deal_id, H)
    await process(oc)
    async with oc.ctx.sessionmaker() as s:
        cursor = await s.get(ChainCursor, (TRON, "deposits"))
        assert cursor is not None
        assert cursor.cursor_final == "200"
