"""Deal service on a real database: lifecycle, authorization, money effects, timers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ChainsConfig, Settings
from app.db.models import Deal, Dispute, IncomingTransfer, PaymentRequest, Payout, User
from app.domain.enums import (
    DealResolution,
    DealStatus,
    DisputeDecision,
    DisputeReason,
    DisputeStatus,
    FeePayer,
    MatchStatus,
    Network,
    PartyRole,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
    TransferStatus,
)
from app.domain.errors import (
    Forbidden,
    InvalidTransition,
    LimitExceeded,
    NotFound,
    ValidationFailed,
    VersionConflict,
)
from app.services.deals import DealChanges, DealService
from tests import addresses
from tests.integration.deal_world import ADMIN, BUYER, SELLER, SELLER_ADDRESSES, STRANGER, World

U = 10**6


@pytest.fixture
async def world(session: AsyncSession, settings: Settings, chains: ChainsConfig) -> World:
    w = World(session, settings, chains)
    await w.setup_users()
    return w


# ── happy path and audit ────────────────────────────────────────────────────


async def test_full_cryptobot_lifecycle(world: World) -> None:
    svc = world.svc
    deal = await world.new_deal()
    assert deal.status is DealStatus.CREATED
    assert (deal.buyer_pays, deal.seller_receives, deal.fee_amount) == (102 * U, 100 * U, 2 * U)

    await svc.accept_deal(SELLER, deal.id, version=deal.version)
    assert deal.status is DealStatus.ACCEPTED
    request = await svc.start_payment(BUYER, deal.id)
    assert deal.status is DealStatus.AWAITING_PAYMENT
    assert request.expected_amount == 102 * U

    await world.deposit(deal, request.expected_amount)
    await svc.fund(deal.id)
    assert deal.status is DealStatus.FUNDED
    assert deal.execution_deadline_at == world.clock.now + timedelta(hours=24)

    await svc.seller_confirm(SELLER, deal.id)
    assert deal.status is DealStatus.SELLER_CONFIRMED

    await svc.buyer_confirm(BUYER, deal.id)
    assert deal.status is DealStatus.PAYOUT_PENDING
    [payout] = await world.payouts(deal)
    assert (payout.kind, payout.status, payout.amount) == (
        PayoutKind.SELLER_PAYOUT,
        PayoutStatus.QUEUED,
        100 * U,
    )
    assert payout.recipient_user_id == SELLER  # CryptoBot pays to the Telegram account
    assert payout.idempotency_key == f"seller_payout:{deal.code}:1"
    assert await world.escrow(deal) == 0
    assert await world.payable(deal) == 100 * U

    await world.settle_payouts(deal)
    assert deal.status is DealStatus.COMPLETED
    assert deal.closed_at is not None
    seller = await world.session.get(User, SELLER)
    assert seller is not None
    assert seller.deals_completed == 1
    assert seller.volume_usdt == 100 * U

    actions = [(e.action, e.from_status, e.to_status) for e in await world.events(deal)]
    assert [a for a, _, _ in actions] == [
        "create",
        "accept",
        "start_payment",
        "fund",
        "seller_confirm",
        "buyer_confirm",
        "start_payout",
        "complete",
    ]


async def test_audit_log_hash_chain(world: World) -> None:
    deal = await world.funded_deal()
    events = await world.events(deal)
    prev = b"\x00" * 32
    for event in events:
        assert event.prev_hash == (None if event.seq == 1 else prev)
        fields = {
            "deal_id": event.deal_id,
            "seq": event.seq,
            "actor_type": event.actor_type.value,
            "actor_id": event.actor_id,
            "action": event.action,
            "from": event.from_status.value if event.from_status else None,
            "to": event.to_status.value if event.to_status else None,
            "tx_hash": event.tx_hash,
            "payload": event.payload,
            "at": event.created_at.isoformat(),
        }
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
        assert hashlib.sha256(prev + canonical.encode()).digest() == event.hash
        prev = event.hash
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


async def test_both_sides_are_notified(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=deal.version)
    assert "deal_offer" in await world.notices(SELLER)
    assert "deal_accepted" in await world.notices(BUYER)


# ── confirmation rules ──────────────────────────────────────────────────────


async def test_buyer_cannot_confirm_before_seller(world: World) -> None:
    deal = await world.funded_deal()
    with pytest.raises(InvalidTransition):
        await world.svc.buyer_confirm(BUYER, deal.id)
    assert await world.payouts(deal) == []


async def test_seller_cannot_confirm_receipt_for_buyer(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.seller_confirm(SELLER, deal.id)
    for intruder in (SELLER, STRANGER, ADMIN):
        with pytest.raises(Forbidden):
            await world.svc.buyer_confirm(intruder, deal.id)
    assert await world.payouts(deal) == []


async def test_double_press_creates_one_payout(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.seller_confirm(SELLER, deal.id)
    await world.svc.buyer_confirm(BUYER, deal.id)
    with pytest.raises(InvalidTransition):
        await world.svc.buyer_confirm(BUYER, deal.id)
    with pytest.raises(InvalidTransition):
        await world.svc.seller_confirm(SELLER, deal.id)
    assert len(await world.payouts(deal)) == 1


async def test_concurrent_confirmations_are_serialized(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, chains: ChainsConfig
) -> None:
    async with sessionmaker() as s, s.begin():
        w = World(s, settings, chains)
        await w.setup_users()
        deal = await w.funded_deal()
        await w.svc.seller_confirm(SELLER, deal.id)
        deal_id = deal.id

    async def press() -> str:
        async with sessionmaker() as s:
            try:
                async with s.begin():
                    await DealService(s, settings, chains).buyer_confirm(BUYER, deal_id)
                    await asyncio.sleep(0.05)
                return "ok"
            except InvalidTransition:
                return "already"

    assert sorted(await asyncio.gather(press(), press(), press())) == ["already", "already", "ok"]
    async with sessionmaker() as s:
        count = await s.scalar(
            sa.select(sa.func.count()).select_from(Payout).where(Payout.deal_id == deal_id)
        )
        assert count == 1


# ── creation, invitation and acceptance ─────────────────────────────────────


async def test_self_deal_rejected(world: World) -> None:
    with pytest.raises(ValidationFailed, match="yourself"):
        await world.new_deal(counterparty=BUYER)
    with pytest.raises(ValidationFailed, match="yourself"):
        await world.new_deal(counterparty_username="@Alice")


@pytest.mark.parametrize(("amount", "kind"), [("4.99", "min"), ("300.01", "max")])
async def test_amount_limits_for_new_users(world: World, amount: str, kind: str) -> None:
    with pytest.raises(LimitExceeded) as err:
        await world.new_deal(amount=amount)
    assert err.value.context["kind"] == kind


async def test_experienced_users_have_higher_limit(world: World) -> None:
    await world.session.execute(sa.update(User).values(deals_completed=5))
    deal = await world.new_deal(amount="5000")
    assert deal.amount == 5000 * U
    with pytest.raises(LimitExceeded):
        await world.new_deal(amount="10000.01")


async def test_counterparty_limit_checked_on_accept(world: World) -> None:
    await world.session.execute(sa.update(User).where(User.id == BUYER).values(deals_completed=5))
    deal = await world.new_deal(amount="1000")  # the buyer may, the new seller may not
    with pytest.raises(LimitExceeded):
        await world.svc.accept_deal(SELLER, deal.id, version=deal.version)


async def test_invited_user_only(world: World) -> None:
    deal = await world.new_deal()
    with pytest.raises(Forbidden):
        await world.svc.accept_deal(STRANGER, deal.id, version=deal.version)
    with pytest.raises(Forbidden):
        await world.svc.accept_deal(BUYER, deal.id, version=deal.version)  # creator


async def test_unknown_username_is_matched_on_accept(world: World) -> None:
    deal = await world.new_deal(counterparty_username="@NewGuy")
    assert deal.invited_user_id is None
    assert deal.invited_username == "NewGuy"
    with pytest.raises(Forbidden):
        await world.svc.accept_deal(STRANGER, deal.id, version=deal.version)  # @mallory is not @newguy
    await world.session.execute(sa.update(User).where(User.id == STRANGER).values(username="newguy"))
    await world.svc.accept_deal(STRANGER, deal.id, version=deal.version)
    assert deal.seller_id == STRANGER


async def test_open_link_needs_creator_confirmation(world: World) -> None:
    deal = await world.new_deal(counterparty=None, counterparty_username="")
    await world.svc.accept_deal(STRANGER, deal.id, version=deal.version)
    assert deal.status is DealStatus.CREATED
    assert deal.pending_counterparty_id == STRANGER
    assert "confirm_counterparty" in await world.notices(BUYER)
    with pytest.raises(Forbidden, match="already taken"):
        await world.svc.accept_deal(SELLER, deal.id, version=deal.version)

    await world.svc.confirm_counterparty(BUYER, deal.id, approve=False)
    assert deal.pending_counterparty_id is None
    assert "counterparty_rejected" in await world.notices(STRANGER)

    await world.svc.accept_deal(SELLER, deal.id, version=deal.version)
    await world.svc.confirm_counterparty(BUYER, deal.id, approve=True)
    assert deal.status is DealStatus.ACCEPTED
    assert deal.seller_id == SELLER


async def test_invite_token_lookup(world: World) -> None:
    deal = await world.new_deal()
    assert deal.invite_token
    assert (await world.svc.open_invite(SELLER, deal.invite_token)).id == deal.id
    with pytest.raises(Forbidden):
        await world.svc.open_invite(STRANGER, deal.invite_token)
    with pytest.raises(NotFound):
        await world.svc.open_invite(SELLER, "nope")


async def test_edit_requires_reacceptance(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    assert deal.status is DealStatus.ACCEPTED

    await world.svc.edit_deal(BUYER, deal.id, version=1, changes=DealChanges(amount=Decimal("150")))
    assert (deal.status, deal.version, deal.amount, deal.buyer_pays) == (
        DealStatus.CREATED,
        2,
        150 * U,
        153 * U,
    )
    assert "deal_edited" in await world.notices(SELLER)

    with pytest.raises(VersionConflict):
        await world.svc.accept_deal(SELLER, deal.id, version=1)  # stale card
    await world.svc.accept_deal(SELLER, deal.id, version=2)
    assert deal.status is DealStatus.ACCEPTED


async def test_only_creator_edits_and_only_before_payment(world: World) -> None:
    deal = await world.new_deal()
    with pytest.raises(Forbidden):
        await world.svc.edit_deal(SELLER, deal.id, version=1, changes=DealChanges(terms="другие условия"))
    funded = await world.funded_deal()
    with pytest.raises(InvalidTransition):
        await world.svc.edit_deal(
            BUYER, funded.id, version=funded.version, changes=DealChanges(terms="x" * 10)
        )


async def test_onchain_seller_must_give_valid_payout_address(world: World) -> None:
    deal = await world.new_deal(network=Network.TRON)
    with pytest.raises(ValidationFailed, match="payout address"):
        await world.svc.accept_deal(SELLER, deal.id, version=deal.version)
    with pytest.raises(ValidationFailed):
        await world.svc.accept_deal(SELLER, deal.id, version=deal.version, payout_address="TNotAnAddress")
    own = world.chains.networks[Network.TRON].deposit_address
    with pytest.raises(ValidationFailed, match="own address"):
        await world.svc.accept_deal(SELLER, deal.id, version=deal.version, payout_address=own)
    await world.svc.accept_deal(
        SELLER, deal.id, version=deal.version, payout_address=SELLER_ADDRESSES[Network.TRON]
    )
    assert deal.seller_payout_address == SELLER_ADDRESSES[Network.TRON]


async def test_payout_address_locked_after_funding(world: World) -> None:
    deal = await world.funded_deal(network=Network.BSC)
    with pytest.raises(InvalidTransition):
        await world.svc.set_payout_address(SELLER, deal.id, addresses.evm(99))


async def test_bep20_amounts_use_18_decimals(world: World) -> None:
    deal = await world.funded_deal(network=Network.BSC)
    assert deal.decimals == 18
    assert deal.amount == 100 * 10**18
    assert deal.seller_receives == 100 * 10**18 - 10**17  # 0.10 network fee on the seller
    await world.svc.seller_confirm(SELLER, deal.id)
    await world.svc.buyer_confirm(BUYER, deal.id)
    [payout] = await world.payouts(deal)
    assert payout.amount == deal.seller_receives
    assert payout.recipient_address == SELLER_ADDRESSES[Network.BSC]


# ── cancellation and refunds ────────────────────────────────────────────────


async def test_cancel_before_payment(world: World) -> None:
    deal = await world.new_deal()
    with pytest.raises(Forbidden):
        await world.svc.cancel(STRANGER, deal.id)
    await world.svc.cancel(SELLER, deal.id)  # the invited side may decline
    assert deal.status is DealStatus.CANCELLED
    assert "deal_cancelled" in await world.notices(BUYER)


async def test_cancel_while_awaiting_payment_releases_invoice(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    request = await world.svc.start_payment(BUYER, deal.id)
    await world.svc.cancel(BUYER, deal.id)
    assert deal.status is DealStatus.CANCELLED
    assert request.status is PaymentRequestStatus.CANCELLED
    assert world.gateway is not None
    assert world.gateway.cancelled == [request.id]


async def test_no_unilateral_cancel_after_funding(world: World) -> None:
    deal = await world.funded_deal()
    with pytest.raises(InvalidTransition):
        await world.svc.cancel(BUYER, deal.id)
    await world.svc.request_cancel(BUYER, deal.id)
    assert deal.status is DealStatus.FUNDED
    assert "cancel_requested" in await world.notices(SELLER)
    await world.svc.revoke_cancel(BUYER, deal.id)
    await world.svc.request_cancel(SELLER, deal.id)
    assert deal.status is DealStatus.FUNDED  # the buyer's earlier request was revoked


async def test_mutual_cancel_refunds_buyer_cryptobot(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.request_cancel(BUYER, deal.id)
    await world.svc.request_cancel(SELLER, deal.id)
    assert (deal.status, deal.resolution) == (DealStatus.REFUND_PENDING, DealResolution.MUTUAL_CANCEL)
    [refund] = await world.payouts(deal)
    assert (refund.kind, refund.recipient_user_id, refund.amount) == (PayoutKind.BUYER_REFUND, BUYER, 102 * U)
    await world.settle_payouts(deal)
    assert deal.status is DealStatus.REFUNDED


async def test_mutual_cancel_onchain_waits_for_refund_address(world: World) -> None:
    deal = await world.funded_deal(network=Network.TRON)
    await world.svc.request_cancel(SELLER, deal.id)
    await world.svc.request_cancel(BUYER, deal.id)
    [refund] = await world.payouts(deal)
    # never refunded to tx.from (could be an exchange hot wallet): the buyer must give an address
    assert refund.status is PayoutStatus.AWAITING_ADDRESS
    assert refund.amount == 102 * U - 1_500_000  # minus the TRC-20 refund network fee
    assert "refund_address_needed" in await world.notices(BUYER)
    with pytest.raises(Forbidden):
        await world.svc.provide_refund_address(SELLER, deal.id, addresses.tron(5))
    await world.svc.provide_refund_address(BUYER, deal.id, addresses.tron(5))
    assert (refund.status, refund.recipient_address) == (PayoutStatus.QUEUED, addresses.tron(5))


async def test_refund_request_flow(world: World) -> None:
    deal = await world.funded_deal()
    with pytest.raises(InvalidTransition, match="deadline"):
        await world.svc.request_refund(BUYER, deal.id)
    world.clock.advance(timedelta(hours=25))
    await world.svc.request_refund(BUYER, deal.id)
    assert "refund_requested" in await world.notices(SELLER)
    assert deal.next_action_at == world.clock.now + world.settings.refund_objection_window
    world.clock.advance(world.settings.refund_objection_window)
    assert await world.run_due_timers() == 1
    assert (deal.status, deal.resolution) == (DealStatus.REFUND_PENDING, DealResolution.REFUND_AFTER_DEADLINE)


async def test_seller_can_agree_to_refund(world: World) -> None:
    deal = await world.funded_deal()
    world.clock.advance(timedelta(hours=25))
    await world.svc.request_refund(BUYER, deal.id)
    await world.svc.agree_refund(SELLER, deal.id)
    assert deal.status is DealStatus.REFUND_PENDING


async def test_seller_delivering_after_refund_request_opens_dispute(world: World) -> None:
    deal = await world.funded_deal()
    world.clock.advance(timedelta(hours=25))
    await world.svc.request_refund(BUYER, deal.id)
    await world.svc.seller_confirm(SELLER, deal.id)
    assert deal.status is DealStatus.DISPUTED
    dispute = (await world.session.execute(sa.select(Dispute).where(Dispute.deal_id == deal.id))).scalar_one()
    assert dispute.reason is DisputeReason.REFUND_OBJECTION
    with pytest.raises(Forbidden):
        await world.svc.withdraw_dispute(SELLER, deal.id)


# ── payment edge cases ──────────────────────────────────────────────────────


async def test_overpayment_is_refunded(world: World) -> None:
    deal = await world.new_deal(network=Network.TRON)
    await world.svc.accept_deal(SELLER, deal.id, version=1, payout_address=SELLER_ADDRESSES[Network.TRON])
    request = await world.svc.start_payment(BUYER, deal.id)
    await world.deposit(deal, request.expected_amount + 20 * U)
    await world.svc.fund(deal.id)
    [refund] = await world.payouts(deal)
    assert (refund.kind, refund.amount) == (PayoutKind.OVERPAY_REFUND, 20 * U - 1_500_000)
    assert await world.escrow(deal) == request.expected_amount


async def test_small_overpayment_goes_to_seller(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    request = await world.svc.start_payment(BUYER, deal.id)
    await world.deposit(deal, request.expected_amount + 500_000)
    await world.svc.fund(deal.id)
    assert await world.payouts(deal) == []
    assert deal.seller_receives == 100 * U + 500_000


async def test_fund_requires_full_amount(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    request = await world.svc.start_payment(BUYER, deal.id)
    await world.deposit(deal, request.expected_amount - 1)
    with pytest.raises(InvalidTransition, match="does not cover"):
        await world.svc.fund(deal.id)


async def test_seller_accepts_underpayment(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    await world.svc.start_payment(BUYER, deal.id)
    await world.deposit(deal, 101 * U)  # an exchange kept 1 USDT
    with pytest.raises(Forbidden):
        await world.svc.accept_underpayment(BUYER, deal.id)
    await world.svc.accept_underpayment(SELLER, deal.id)
    assert deal.status is DealStatus.FUNDED
    assert deal.seller_receives == 99 * U
    await world.svc.seller_confirm(SELLER, deal.id)
    await world.svc.buyer_confirm(BUYER, deal.id)
    [payout] = await world.payouts(deal)
    assert payout.amount == 99 * U  # the service fee is untouched, the seller bore the shortfall


async def test_payment_expiry_without_money(world: World) -> None:
    deal = await world.new_deal(network=Network.TRON)
    await world.svc.accept_deal(SELLER, deal.id, version=1, payout_address=SELLER_ADDRESSES[Network.TRON])
    request = await world.svc.start_payment(BUYER, deal.id)
    world.clock.advance(world.settings.payment_ttl)
    assert await world.run_due_timers() == 1
    assert deal.status is DealStatus.EXPIRED
    assert request.status is PaymentRequestStatus.EXPIRED
    assert request.tag_release_at == world.clock.now + world.settings.tag_cooldown


async def test_payment_expiry_deferred_while_transfer_confirms(world: World) -> None:
    deal = await world.new_deal(network=Network.TRON)
    await world.svc.accept_deal(SELLER, deal.id, version=1, payout_address=SELLER_ADDRESSES[Network.TRON])
    await world.svc.start_payment(BUYER, deal.id)
    world.session.add(
        IncomingTransfer(
            provider=Provider.ONCHAIN,
            network=Network.TRON,
            asset="USDT",
            tx_hash="ab" * 32,
            event_index=0,
            to_address="T",
            amount=102 * U,
            status=TransferStatus.SEEN,
            match_status=MatchStatus.PENDING,
            deal_id=deal.id,
        )
    )
    await world.session.flush()
    world.clock.advance(world.settings.payment_ttl)
    await world.run_due_timers()
    assert deal.status is DealStatus.AWAITING_PAYMENT
    assert deal.next_action_at is not None
    assert deal.next_action_at > world.clock.now


async def test_payment_expiry_with_partial_money_refunds(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    await world.svc.start_payment(BUYER, deal.id)
    await world.deposit(deal, 40 * U)
    world.clock.advance(world.settings.payment_ttl)
    await world.run_due_timers()
    assert (deal.status, deal.resolution) == (
        DealStatus.REFUND_PENDING,
        DealResolution.PARTIAL_PAYMENT_RETURNED,
    )
    [refund] = await world.payouts(deal)
    assert refund.amount == 40 * U


async def test_late_payment_after_expiry_is_refunded(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    await world.svc.start_payment(BUYER, deal.id)
    world.clock.advance(world.settings.payment_ttl)
    await world.run_due_timers()
    assert deal.status is DealStatus.EXPIRED
    await world.deposit(deal, 102 * U)
    await world.svc.on_late_payment(deal.id, tx_hash="late")
    assert (deal.status, deal.resolution) == (DealStatus.REFUND_PENDING, DealResolution.LATE_PAYMENT)
    [refund] = await world.payouts(deal)
    assert refund.amount == 102 * U


async def test_offer_expires(world: World) -> None:
    deal = await world.new_deal()
    world.clock.advance(world.settings.offer_ttl - timedelta(seconds=1))
    assert await world.run_due_timers() == 0
    world.clock.advance(timedelta(seconds=1))
    assert await world.run_due_timers() == 1
    assert deal.status is DealStatus.EXPIRED


async def test_payment_window_extension_once(world: World) -> None:
    deal = await world.new_deal()
    await world.svc.accept_deal(SELLER, deal.id, version=1)
    request = await world.svc.start_payment(BUYER, deal.id)
    first_expiry = request.expires_at
    await world.svc.extend_payment(BUYER, deal.id)
    assert request.expires_at == first_expiry + world.settings.payment_extension
    with pytest.raises(LimitExceeded):
        await world.svc.extend_payment(BUYER, deal.id)


# ── buyer silence and disputes ──────────────────────────────────────────────


async def test_buyer_silence_never_pays_seller(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.seller_confirm(SELLER, deal.id)
    for _ in range(2):
        world.clock.advance(timedelta(hours=24))
        assert await world.run_due_timers() == 1
    assert (await world.notices(BUYER)).count("buyer_reminder") == 2
    world.clock.advance(timedelta(hours=24))
    await world.run_due_timers()
    assert deal.status is DealStatus.DISPUTED  # 72 h of silence → dispute, not payout
    assert await world.payouts(deal) == []
    assert "admin_dispute_opened" in await world.notices(ADMIN)


async def test_withdrawn_timeout_dispute_restarts_the_clock(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.seller_confirm(SELLER, deal.id)
    world.clock.advance(timedelta(hours=72))  # the worker was down: one tick goes straight to the dispute
    assert await world.run_due_timers() == 1
    assert deal.status is DealStatus.DISPUTED
    await world.svc.withdraw_dispute(BUYER, deal.id)
    assert deal.status is DealStatus.SELLER_CONFIRMED
    assert deal.next_action_at == world.clock.now + timedelta(hours=24)
    await world.svc.buyer_confirm(BUYER, deal.id)
    assert deal.status is DealStatus.PAYOUT_PENDING


async def test_dispute_only_after_funding(world: World) -> None:
    deal = await world.new_deal()
    with pytest.raises(InvalidTransition):
        await world.svc.open_dispute(BUYER, deal.id, "не пришло")


async def test_dispute_split_single_admin_decision(world: World) -> None:
    await world.session.execute(sa.update(User).values(deals_completed=5))
    deal = await world.funded_deal(amount="5000")
    await world.svc.open_dispute(SELLER, deal.id, "покупатель не отвечает")
    assert deal.status is DealStatus.DISPUTED
    with pytest.raises(Forbidden):
        await world.svc.resolve_dispute(
            STRANGER, deal.id, decision=DisputeDecision.SPLIT, buyer_share_bps=5000, comment="ok"
        )
    with pytest.raises(ValidationFailed, match="comment"):
        await world.svc.resolve_dispute(
            ADMIN, deal.id, decision=DisputeDecision.SPLIT, buyer_share_bps=5000, comment=""
        )
    preview = await world.svc.preview_resolution(
        deal.id, decision=DisputeDecision.SPLIT, buyer_share_bps=5000
    )
    await world.svc.resolve_dispute(
        ADMIN,
        deal.id,
        decision=DisputeDecision.SPLIT,
        buyer_share_bps=5000,
        comment="Половина работы выполнена",
    )
    assert deal.status is DealStatus.PAYOUT_PENDING
    payouts = {p.kind: p for p in await world.payouts(deal)}
    assert payouts[PayoutKind.SPLIT_BUYER].amount == preview.settlement.to_buyer
    assert payouts[PayoutKind.SPLIT_SELLER].amount == preview.settlement.to_seller
    # one admin decides: even 2 500 USDT payouts need no second approval
    assert all(p.status is PayoutStatus.QUEUED for p in payouts.values())
    dispute = (await world.session.execute(sa.select(Dispute).where(Dispute.deal_id == deal.id))).scalar_one()
    assert (dispute.status, dispute.arbitrator_id) == (DisputeStatus.RESOLVED, ADMIN)
    await world.settle_payouts(deal)
    assert deal.status is DealStatus.COMPLETED


async def test_dispute_for_buyer_refunds(world: World) -> None:
    deal = await world.funded_deal()
    await world.svc.open_dispute(BUYER, deal.id, "товар не передан")
    await world.svc.resolve_dispute(
        ADMIN,
        deal.id,
        decision=DisputeDecision.BUYER,
        keep_fee=False,
        comment="Продавец не подтвердил передачу",
    )
    assert deal.status is DealStatus.REFUND_PENDING
    [refund] = await world.payouts(deal)
    assert (refund.kind, refund.amount) == (PayoutKind.BUYER_REFUND, 102 * U)
    seller = await world.session.get(User, SELLER)
    assert seller is not None
    assert seller.disputes_lost == 1


async def test_admin_cannot_decide_own_deal(world: World) -> None:
    await world.session.execute(sa.update(User).where(User.id == SELLER).values(role="admin"))
    deal = await world.funded_deal()
    await world.svc.open_dispute(BUYER, deal.id)
    with pytest.raises(Forbidden, match="own deal"):
        await world.svc.resolve_dispute(SELLER, deal.id, decision=DisputeDecision.SELLER, comment="мне")


async def test_large_regular_payout_needs_one_approval(world: World) -> None:
    await world.session.execute(sa.update(User).values(deals_completed=5))
    deal = await world.funded_deal(amount="2500")
    await world.svc.seller_confirm(SELLER, deal.id)
    await world.svc.buyer_confirm(BUYER, deal.id)
    [payout] = await world.payouts(deal)
    assert (payout.status, payout.approvals_required) == (PayoutStatus.AWAITING_APPROVAL, 1)
    assert "admin_payout_approval" in await world.notices(ADMIN)


async def test_deals_are_private(world: World) -> None:
    deal = await world.new_deal()
    with pytest.raises(NotFound):
        await world.svc.get_for_user(STRANGER, deal.id)
    assert (await world.svc.get_for_user(ADMIN, deal.id)).id == deal.id


async def test_seller_creator_onchain(world: World) -> None:
    deal = await world.funded_deal(
        network=Network.SOL, creator_role=PartyRole.SELLER, fee_payer=FeePayer.SPLIT
    )
    assert deal.seller_payout_address == SELLER_ADDRESSES[Network.SOL]
    assert deal.buyer_pays == 101 * U
    assert (await world.session.get(PaymentRequest, 1)) is not None
    assert deal.status is DealStatus.FUNDED


async def test_banned_user_cannot_act(world: World) -> None:
    deal = await world.new_deal()
    await world.session.execute(sa.update(User).where(User.id == SELLER).values(is_banned=True))
    with pytest.raises(Forbidden):
        await world.svc.accept_deal(SELLER, deal.id, version=1)
    assert (await world.session.get(Deal, deal.id)).status is DealStatus.CREATED  # type: ignore[union-attr]
