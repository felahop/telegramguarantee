"""Admin operations on a real database (payments through the in-memory CryptoBot)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.models import AdminAudit, IncomingTransfer, Payout, PayoutApproval, User
from app.domain.enums import (
    DealStatus,
    DisputeStatus,
    MatchMethod,
    MatchStatus,
    PayoutKind,
    PayoutStatus,
    UserRole,
)
from app.domain.errors import Forbidden, InvalidTransition, ValidationFailed
from app.services.admin import AdminService
from app.services.ledger import AccountCode, AccountRef
from app.services.payments import ApplyResult
from app.worker.cryptobot import CryptoBotPayouts
from tests.integration.cb_world import CB, USDT, awaiting, confirmed_deal
from tests.integration.deal_world import ADMIN, BUYER, SELLER

U = 10**6


def svc(cb: CB, session: object) -> AdminService:
    return AdminService(session, cb.ctx.settings, cb.ctx.chains, clock=cb.clock, gateway=cb.ctx.gateway)  # type: ignore[arg-type]


async def large_payout(cb: CB) -> tuple[int, Payout]:
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.update(User).values(deals_completed=5))
    deal_id = await confirmed_deal(cb, amount="2500")
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.AWAITING_APPROVAL
    return deal_id, payout


async def unidentified_underpayment(cb: CB) -> tuple[int, int]:
    """Buyer paid 101 into a 102 invoice → the money sits in unidentified. Returns (deal, transfer)."""
    deal_id, invoice_id = await awaiting(cb)
    assert await cb.apply(cb.fake.pay(invoice_id, amount="101")) is ApplyResult.ANOMALY
    async with cb.ctx.sessionmaker() as s:
        transfer = (
            await s.execute(
                sa.select(IncomingTransfer).where(IncomingTransfer.match_status == MatchStatus.UNIDENTIFIED)
            )
        ).scalar_one()
    return deal_id, transfer.id


# ── payouts ──────────────────────────────────────────────────────────────────


async def test_approve_large_payout(cb: CB) -> None:
    _, payout = await large_payout(cb)
    async with cb.ctx.uow() as uow:
        approved = await svc(cb, uow.session).approve_payout(ADMIN, payout.id)
    assert approved.status is PayoutStatus.QUEUED
    assert await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert cb.fake.calls_to("transfer")[0]["amount"] == "2500"
    async with cb.ctx.sessionmaker() as s:
        assert (await s.execute(sa.select(AdminAudit.action))).scalars().all() == ["approve_payout"]


async def test_hold_then_approve(cb: CB) -> None:
    _, payout = await large_payout(cb)
    async with cb.ctx.uow() as uow:
        held = await svc(cb, uow.session).reject_payout(ADMIN, payout.id, "проверить переписку")
    assert held.status is PayoutStatus.AWAITING_APPROVAL
    assert "проверить переписку" in (held.last_error or "")
    async with cb.ctx.uow() as uow:
        approved = await svc(cb, uow.session).approve_payout(ADMIN, payout.id)
    assert approved.status is PayoutStatus.QUEUED
    async with cb.ctx.sessionmaker() as s:
        [approval] = list((await s.execute(sa.select(PayoutApproval))).scalars())
        assert approval.decision.value == "approve"


async def test_only_uninvolved_admins_approve(cb: CB) -> None:
    _, payout = await large_payout(cb)
    with pytest.raises(Forbidden):
        async with cb.ctx.uow() as uow:
            await svc(cb, uow.session).approve_payout(BUYER, payout.id)  # not an admin
    async with cb.ctx.sessionmaker() as s, s.begin():
        await s.execute(sa.update(User).where(User.id == SELLER).values(role=UserRole.ADMIN))
    with pytest.raises(Forbidden, match="own deal"):
        async with cb.ctx.uow() as uow:
            await svc(cb, uow.session).approve_payout(SELLER, payout.id)


async def test_retry_failed_payout(cb: CB) -> None:
    deal_id = await confirmed_deal(cb)
    cb.fake.errors["transfer"].append("METHOD_DISABLED")
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    [payout] = await cb.payouts(deal_id)
    assert payout.status is PayoutStatus.FAILED_FINAL
    async with cb.ctx.uow() as uow:
        await svc(cb, uow.session).retry_payout(ADMIN, payout.id)
    await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert (await cb.deal(deal_id)).status is DealStatus.COMPLETED


# ── unidentified payments ────────────────────────────────────────────────────


async def test_assign_underpayment_then_seller_accepts(cb: CB) -> None:
    deal_id, transfer_id = await unidentified_underpayment(cb)
    deal = await cb.deal(deal_id)
    async with cb.ctx.uow() as uow:
        candidates = await svc(cb, uow.session).candidates(
            await uow.session.get(IncomingTransfer, transfer_id)
        )  # type: ignore[arg-type]
        assert [d.id for d in candidates] == [deal_id]
        await svc(cb, uow.session).assign_transfer(ADMIN, transfer_id, deal.code.lower())
    assert await cb.balance(AccountRef.escrow(USDT, deal_id)) == 101 * U
    assert await cb.balance(AccountRef.unidentified(USDT)) == 0
    assert (await cb.deal(deal_id)).status is DealStatus.AWAITING_PAYMENT  # 101 of 102
    async with cb.ctx.uow() as uow:
        await uow.deals.accept_underpayment(SELLER, deal_id)
    assert (await cb.deal(deal_id)).status is DealStatus.FUNDED
    async with cb.ctx.sessionmaker() as s:
        transfer = await s.get(IncomingTransfer, transfer_id)
        assert transfer is not None
        assert (transfer.match_status, transfer.match_method, transfer.deal_id) == (
            MatchStatus.MATCHED,
            MatchMethod.ADMIN,
            deal_id,
        )


async def test_assign_to_expired_deal_refunds(cb: CB) -> None:
    deal_id, transfer_id = await unidentified_underpayment(cb)
    async with cb.ctx.uow() as uow:
        await uow.deals.cancel(BUYER, deal_id)
    code = (await cb.deal(deal_id)).code
    async with cb.ctx.uow() as uow:
        await svc(cb, uow.session).assign_transfer(ADMIN, transfer_id, code)
    assert (await cb.deal(deal_id)).status is DealStatus.REFUND_PENDING
    [refund] = await cb.payouts(deal_id)
    assert (refund.kind, refund.amount) == (PayoutKind.BUYER_REFUND, 101 * U)


async def test_assign_to_funded_deal_is_refused_and_rolled_back(cb: CB) -> None:
    other_deal, other_invoice = await awaiting(cb)
    await cb.apply(cb.fake.pay(other_invoice))  # this one is funded
    _, transfer_id = await unidentified_underpayment(cb)
    code = (await cb.deal(other_deal)).code
    with pytest.raises(InvalidTransition):
        async with cb.ctx.uow() as uow:
            await svc(cb, uow.session).assign_transfer(ADMIN, transfer_id, code)
    assert await cb.balance(AccountRef.unidentified(USDT)) == 101 * U
    with pytest.raises(InvalidTransition):  # the same payment cannot be assigned twice either
        async with cb.ctx.uow() as uow:
            deal_id, _ = await awaiting(cb)
            await svc(cb, uow.session).assign_transfer(ADMIN, transfer_id, (await cb.deal(deal_id)).code)
            await svc(cb, uow.session).assign_transfer(ADMIN, transfer_id, (await cb.deal(deal_id)).code)


async def test_return_unidentified_to_a_telegram_user(cb: CB) -> None:
    _, transfer_id = await unidentified_underpayment(cb)
    with pytest.raises(ValidationFailed):
        async with cb.ctx.uow() as uow:
            await svc(cb, uow.session).return_transfer(ADMIN, transfer_id)
    async with cb.ctx.uow() as uow:
        payout = await svc(cb, uow.session).return_transfer(ADMIN, transfer_id, recipient_user_id=BUYER)
    assert (payout.kind, payout.amount, payout.deal_id) == (PayoutKind.UNIDENTIFIED_RETURN, 101 * U, None)
    assert await cb.balance(AccountRef(AccountCode.PAYABLE_UNIDENTIFIED, USDT)) == 101 * U
    assert await CryptoBotPayouts(cb.ctx, cb.client).run_once()
    assert cb.fake.calls_to("transfer")[0]["user_id"] == BUYER
    assert await cb.balance(AccountRef(AccountCode.PAYABLE_UNIDENTIFIED, USDT)) == 0
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 0


# ── overview ─────────────────────────────────────────────────────────────────


async def test_counts_balances_and_search(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id))
    await unidentified_underpayment(cb)
    deal = await cb.deal(deal_id)
    async with cb.ctx.uow() as uow:
        admin = svc(cb, uow.session)
        counts = await admin.counts()
        assert (counts.unidentified, counts.disputes, counts.approvals) == (1, 0, 0)
        [row] = await admin.balances()
        assert row.asset == USDT
        assert row.custody["cryptobot"] == 203 * U
        assert row.get(AccountCode.ESCROW) == 102 * U
        assert row.get(AccountCode.UNIDENTIFIED) == 101 * U
        assert [d.id for d in (await admin.search(deal.code)).deals] == [deal_id]
        found = await admin.search("@alice")
        assert [u.id for u in found.users] == [BUYER]
        assert {d.id for d in found.deals} >= {deal_id}
        assert len((await admin.search(str(invoice_id))).transfers) == 1


async def test_dispute_queue_take_and_dossier(cb: CB) -> None:
    deal_id, invoice_id = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice_id))
    async with cb.ctx.uow() as uow:
        await uow.deals.open_dispute(BUYER, deal_id, "Не пришёл файл")
    async with cb.ctx.uow() as uow:
        admin = svc(cb, uow.session)
        [(_, deal)] = await admin.open_disputes()
        assert deal.id == deal_id
        taken = await admin.take_dispute(ADMIN, deal_id)
        assert (taken.status, taken.arbitrator_id) == (DisputeStatus.IN_REVIEW, ADMIN)
        await admin.log_arbiter_message(ADMIN, deal_id, "Пришлите подтверждение передачи")
    async with cb.ctx.uow() as uow:
        d = await svc(cb, uow.session).dossier(deal_id)
    assert d.dispute.reason_text == "Не пришёл файл"
    assert d.escrow == 102 * U
    assert [m.sender_role for m in d.messages] == ["arbitrator"]
    assert [e.action for e in d.events][-1] == "open_dispute"
    assert "dispute_in_review" in await cb.notices(BUYER)


async def test_ban_and_unban(cb: CB) -> None:
    async with cb.ctx.uow() as uow:
        user = await svc(cb, uow.session).set_ban(ADMIN, SELLER, banned=True, reason="мошенничество")
        assert (user.is_banned, user.ban_reason) == (True, "мошенничество")
    with pytest.raises(Forbidden):
        async with cb.ctx.uow() as uow:
            await svc(cb, uow.session).set_ban(BUYER, SELLER, banned=False)
    async with cb.ctx.uow() as uow:
        user = await svc(cb, uow.session).set_ban(ADMIN, SELLER, banned=False)
        assert (user.is_banned, user.ban_reason) == (False, None)
