"""Deal service: every operation on a deal, for the parties, the system and admins.

Contract of every public method:
* runs inside the caller's DB transaction (``async with session.begin(): ...``);
* locks the deal row (``SELECT … FOR UPDATE``) before reading its state, so concurrent button
  presses, webhooks and timers are serialized per deal;
* validates the action with the declarative FSM (``app.domain.fsm``);
* writes the audit event (hash-chained), ledger postings, payouts and outbox notifications in the
  same transaction, then recomputes the deal timer.

A repeated button press lands on a deal whose status has already moved on and gets
``InvalidTransition`` — it never creates a second payout or a second ledger entry.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.addresses import same_address, validate_address
from app.config import AssetSpec, ChainsConfig, Settings
from app.db.models import (
    AdminAudit,
    Deal,
    DealEvent,
    DepositAddress,
    Dispute,
    HotWallet,
    IncomingTransfer,
    PaymentRequest,
    Payout,
    User,
)
from app.domain import fsm
from app.domain.enums import (
    BUYER_BOUND_PAYOUTS,
    ActorType,
    DealResolution,
    DealSource,
    DealStatus,
    DisputeDecision,
    DisputeReason,
    DisputeStatus,
    FeePayer,
    Network,
    PartyRole,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
    TimerKind,
    TransferStatus,
    UserRole,
)
from app.domain.errors import (
    AmountError,
    DomainError,
    Forbidden,
    InvalidTransition,
    LimitExceeded,
    NotFound,
    ValidationFailed,
    VersionConflict,
)
from app.domain.fees import (
    BPS,
    FeeBreakdown,
    FeePolicy,
    Settlement,
    calculate_fees,
    service_fee_in_escrow,
    settle_dispute,
    settle_refund,
    settle_release,
)
from app.domain.fsm import Action, Role
from app.domain.money import asset_code, rescale, to_minor
from app.domain.timers import PAYMENT_EXPIRY_DEFERRAL, next_timer
from app.logging import get_logger
from app.services import ledger as postings
from app.services import notifications
from app.services.ledger import AccountRef, Ledger
from app.services.notifications import Notice
from app.services.users import find_by_username, get_active_user, is_blacklisted

log = get_logger(__name__)

_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
_CODE_LENGTH = 8
TERMS_MIN_LENGTH = 3
COMMENT_MIN_LENGTH = 3
USDT_VOLUME_DECIMALS = 6


def utcnow() -> datetime:
    return datetime.now(UTC)


# ── ports and commands ────────────────────────────────────────────────────────


class PaymentGateway(Protocol):
    """Provider-specific payment requests (CryptoBot invoice, on-chain instructions)."""

    async def create_request(self, session: AsyncSession, deal: Deal, *, now: datetime) -> PaymentRequest: ...

    async def extend_request(
        self, session: AsyncSession, request: PaymentRequest, *, new_expires_at: datetime
    ) -> bool: ...

    async def cancel_request(self, session: AsyncSession, request: PaymentRequest) -> None: ...


@dataclass(frozen=True)
class Actor:
    type: ActorType
    user_id: int | None = None

    @classmethod
    def user(cls, user_id: int) -> Actor:
        return cls(ActorType.USER, user_id)

    @classmethod
    def system(cls) -> Actor:
        return cls(ActorType.SYSTEM)

    @classmethod
    def admin(cls, user_id: int) -> Actor:
        return cls(ActorType.ADMIN, user_id)


@dataclass(frozen=True)
class NewDeal:
    creator_id: int
    creator_role: PartyRole
    network: Network
    amount: Decimal  # USDT, as typed by the user
    terms: str
    execution_hours: int
    fee_payer: FeePayer
    counterparty_user_id: int | None = None
    counterparty_username: str | None = None
    seller_payout_address: str | None = None
    source: DealSource = DealSource.WIZARD


@dataclass(frozen=True)
class DealChanges:
    amount: Decimal | None = None
    terms: str | None = None
    execution_hours: int | None = None
    fee_payer: FeePayer | None = None
    seller_payout_address: str | None = None

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.amount,
                self.terms,
                self.execution_hours,
                self.fee_payer,
                self.seller_payout_address,
            )
        )


@dataclass
class ResolutionPreview:
    decision: DisputeDecision
    buyer_share_bps: int
    keep_fee: bool
    escrow: int
    settlement: Settlement


@dataclass
class _Effects:
    """Notifications collected during an operation, flushed with the deal's event sequence."""

    notices: list[tuple[int, Notice, dict[str, Any]]] = field(default_factory=list)


class DealService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        chains: ChainsConfig,
        *,
        gateway: PaymentGateway | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._s = session
        self._settings = settings
        self._chains = chains
        self._gateway = gateway
        self._clock = clock
        self._ledger = Ledger(session)

    # ══ queries ═══════════════════════════════════════════════════════════
    def quote(self, network: Network, amount: Decimal, fee_payer: FeePayer) -> FeeBreakdown:
        spec = self._chains.spec(network)
        return calculate_fees(spec.minor(amount), fee_payer, FeePolicy.from_settings(self._settings, spec))

    async def check_amount(self, user_id: int, network: Network, amount: Decimal) -> int:
        """Early validation for the wizard: limits for this user (the service re-checks on create)."""
        user = await get_active_user(self._s, user_id)
        return self._validate_amount(self._spec(network), amount, user)

    async def check_payout_address(self, network: Network, address: str) -> str:
        """Early validation for the wizard: format, checksum, not our own address, not blacklisted."""
        return await self._validate_payout_address(network, address)

    async def get_for_user(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._get(deal_id)
        if user_id in (
            deal.creator_id,
            deal.buyer_id,
            deal.seller_id,
            deal.invited_user_id,
            deal.pending_counterparty_id,
        ):
            return deal
        user = await self._s.get(User, user_id)
        if user is not None and user.role in (UserRole.ADMIN, UserRole.ARBITRATOR):
            return deal
        raise NotFound("deal not found", deal_id=deal_id)  # do not reveal other people's deals

    async def get_for_invitee(self, user_id: int, deal_id: int) -> Deal:
        """A deal as seen by a party *or* by the user it is offered to (invite checks apply)."""
        deal = await self._get(deal_id)
        if user_id in (deal.creator_id, deal.buyer_id, deal.seller_id):
            return deal
        user = await get_active_user(self._s, user_id)
        if user.role in (UserRole.ADMIN, UserRole.ARBITRATOR):
            return deal
        self._check_invitee(deal, user)
        return deal

    async def open_invite(self, user_id: int, token: str) -> Deal:
        """Resolve a deep-link invite token for a prospective counterparty (no state change)."""
        deal = (await self._s.execute(sa.select(Deal).where(Deal.invite_token == token))).scalar_one_or_none()
        if deal is None:
            raise NotFound("invite not found")
        if user_id == deal.creator_id:
            return deal
        user = await get_active_user(self._s, user_id)
        self._check_invitee(deal, user)
        return deal

    async def escrow_balance(self, deal: Deal) -> int:
        return await self._ledger.normal_balance(AccountRef.escrow(self._asset(deal), deal.id))

    async def due_deal_ids(self, limit: int = 100) -> list[int]:
        rows = await self._s.execute(
            sa.select(Deal.id)
            .where(Deal.next_action_at <= self._clock())
            .order_by(Deal.next_action_at)
            .limit(limit)
        )
        return list(rows.scalars())

    # ══ creation and negotiation ══════════════════════════════════════════
    async def create_deal(self, cmd: NewDeal) -> Deal:
        now = self._clock()
        creator = await get_active_user(self._s, cmd.creator_id)
        spec = self._spec(cmd.network)
        terms = self._validate_terms(cmd.terms)
        hours = self._validate_hours(cmd.execution_hours)
        amount = self._validate_amount(spec, cmd.amount, creator)
        breakdown = calculate_fees(amount, cmd.fee_payer, FeePolicy.from_settings(self._settings, spec))

        invited_user_id, invited_username = await self._resolve_counterparty(cmd)
        payout_address = None
        if cmd.creator_role is PartyRole.SELLER and cmd.network.provider is Provider.ONCHAIN:
            if not cmd.seller_payout_address:
                raise ValidationFailed("payout address is required", field="seller_payout_address")
            payout_address = await self._validate_payout_address(cmd.network, cmd.seller_payout_address)

        deal = Deal(
            invite_token=secrets.token_urlsafe(16),
            status=DealStatus.CREATED,
            source=cmd.source,
            creator_id=creator.id,
            creator_role=cmd.creator_role,
            buyer_id=creator.id if cmd.creator_role is PartyRole.BUYER else None,
            seller_id=creator.id if cmd.creator_role is PartyRole.SELLER else None,
            invited_user_id=invited_user_id,
            invited_username=invited_username,
            terms=terms,
            provider=cmd.network.provider,
            network=cmd.network,
            asset=spec.asset,
            decimals=spec.decimals,
            execution_hours=hours,
            seller_payout_address=payout_address,
            fee_rate_bps=self._settings.fee_rate_bps,
            fee_min=to_minor(self._settings.fee_min_usdt, spec.decimals),
            network_fee_payer=self._settings.network_fee_payer,
            created_at=now,
            event_seq=0,
            version=1,
        )
        self._apply_breakdown(deal, breakdown, cmd.fee_payer)
        await self._insert_with_unique_code(deal)

        effects = _Effects()
        if invited_user_id is not None and await self._s.get(User, invited_user_id) is not None:
            effects.notices.append((invited_user_id, Notice.DEAL_OFFER, {}))
        await self._record(
            deal,
            Actor.user(creator.id),
            "create",
            None,
            DealStatus.CREATED,
            {"amount": amount, "network": cmd.network.value, "source": cmd.source.value},
        )
        await self._finish(deal, effects)
        log.info(
            "deal_created",
            deal_id=deal.id,
            code=deal.code,
            network=cmd.network.value,
            source=cmd.source.value,
        )
        return deal

    async def edit_deal(self, user_id: int, deal_id: int, *, version: int, changes: DealChanges) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        self._check_version(deal, version)
        if changes.is_empty():
            raise ValidationFailed("nothing to change")
        before = deal.status
        fsm.apply(before, Action.EDIT, self._roles(deal, actor))
        creator = await get_active_user(self._s, user_id)
        spec = self._spec(deal.network)

        if changes.terms is not None:
            deal.terms = self._validate_terms(changes.terms)
        if changes.execution_hours is not None:
            deal.execution_hours = self._validate_hours(changes.execution_hours)
        if changes.seller_payout_address is not None:
            if deal.seller_id != user_id or deal.provider is not Provider.ONCHAIN:
                raise Forbidden("only the seller sets the payout address")
            deal.seller_payout_address = await self._validate_payout_address(
                deal.network, changes.seller_payout_address
            )
        amount = (
            self._validate_amount(spec, changes.amount, creator)
            if changes.amount is not None
            else deal.amount
        )
        fee_payer = changes.fee_payer or deal.fee_payer
        breakdown = calculate_fees(amount, fee_payer, FeePolicy.from_settings(self._settings, spec))
        self._apply_breakdown(deal, breakdown, fee_payer)

        deal.version += 1
        deal.status = DealStatus.CREATED
        deal.accepted_at = None
        effects = _Effects()
        if deal.pending_counterparty_id is not None:
            effects.notices.append((deal.pending_counterparty_id, Notice.DEAL_EDITED, {}))
            deal.pending_counterparty_id = None
        other = deal.counterparty_of(user_id) or deal.invited_user_id
        if other is not None:
            effects.notices.append((other, Notice.DEAL_EDITED, {}))
        await self._record(deal, actor, Action.EDIT, before, DealStatus.CREATED, {"version": deal.version})
        await self._finish(deal, effects)
        return deal

    async def accept_deal(
        self, user_id: int, deal_id: int, *, version: int, payout_address: str | None = None
    ) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        self._check_version(deal, version)
        if deal.status is not DealStatus.CREATED:
            raise InvalidTransition("deal is no longer open for acceptance", status=deal.status)
        if user_id == deal.creator_id:
            raise Forbidden("the creator cannot accept their own deal")
        user = await get_active_user(self._s, user_id)
        open_invite = self._check_invitee(deal, user)
        self._check_amount_limit(user, deal)

        role = deal.creator_role.other
        if role is PartyRole.SELLER and deal.provider is Provider.ONCHAIN:
            address = payout_address or deal.seller_payout_address
            if not address:
                raise ValidationFailed("payout address is required", field="seller_payout_address")
            deal.seller_payout_address = await self._validate_payout_address(deal.network, address)

        effects = _Effects()
        roles = {Role.COUNTERPARTY}
        if open_invite:
            # Anyone holding an open link could press "accept" (e.g. in a group chat): the creator
            # must confirm that this is the intended counterparty.
            fsm.apply(deal.status, Action.ACCEPT, roles, target=DealStatus.CREATED)
            deal.pending_counterparty_id = user_id
            effects.notices.append((deal.creator_id, Notice.CONFIRM_COUNTERPARTY, {"user_id": user_id}))
            await self._record(
                deal,
                actor,
                Action.ACCEPT,
                DealStatus.CREATED,
                DealStatus.CREATED,
                {"pending_confirmation": True},
            )
        else:
            self._bind_counterparty(deal, user_id)
            await self._transition(deal, Action.ACCEPT, actor, roles=roles, target=DealStatus.ACCEPTED)
            effects.notices.append((deal.creator_id, Notice.DEAL_ACCEPTED, {}))
            self._inline_card_notice(deal, effects)
        await self._finish(deal, effects)
        return deal

    async def confirm_counterparty(self, user_id: int, deal_id: int, *, approve: bool) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        pending = deal.pending_counterparty_id
        if pending is None:
            raise InvalidTransition("nobody is waiting for confirmation")
        effects = _Effects()
        if approve:
            self._bind_counterparty(deal, pending)
            deal.pending_counterparty_id = None
            await self._transition(deal, Action.CONFIRM_COUNTERPARTY, actor, target=DealStatus.ACCEPTED)
            effects.notices.append((pending, Notice.DEAL_ACCEPTED, {}))
            self._inline_card_notice(deal, effects)
        else:
            fsm.apply(
                deal.status, Action.CONFIRM_COUNTERPARTY, self._roles(deal, actor), target=DealStatus.CREATED
            )
            deal.pending_counterparty_id = None
            if deal.creator_role is PartyRole.BUYER:
                deal.seller_payout_address = None  # the address belonged to the rejected candidate
            await self._record(
                deal,
                actor,
                Action.CONFIRM_COUNTERPARTY,
                deal.status,
                deal.status,
                {"rejected_user_id": pending},
            )
            effects.notices.append((pending, Notice.COUNTERPARTY_REJECTED, {}))
        await self._finish(deal, effects)
        return deal

    async def set_payout_address(self, user_id: int, deal_id: int, address: str) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        if deal.provider is not Provider.ONCHAIN:
            raise ValidationFailed("CryptoBot deals are paid to the seller's Telegram account")
        fsm.apply(deal.status, Action.SET_PAYOUT_ADDRESS, self._roles(deal, actor))
        deal.seller_payout_address = await self._validate_payout_address(deal.network, address)
        effects = _Effects()
        if deal.buyer_id is not None:
            effects.notices.append((deal.buyer_id, Notice.PAYOUT_ADDRESS_CHANGED, {}))
        await self._record(
            deal,
            actor,
            Action.SET_PAYOUT_ADDRESS,
            deal.status,
            deal.status,
            {"address": deal.seller_payout_address},
        )
        await self._finish(deal, effects)
        return deal

    async def provide_refund_address(self, user_id: int, deal_id: int, address: str) -> Deal:
        """Buyer gives the address for an on-chain refund; waiting refund payouts get it once."""
        deal = await self._lock(deal_id)
        if deal.buyer_id != user_id:
            raise Forbidden("only the buyer sets the refund address")
        if deal.provider is not Provider.ONCHAIN:
            raise ValidationFailed("CryptoBot refunds go to the buyer's Telegram account")
        normalized = await self._validate_payout_address(deal.network, address)
        deal.buyer_refund_address = normalized
        waiting = (
            await self._s.execute(
                sa.select(Payout).where(
                    Payout.deal_id == deal.id,
                    Payout.status == PayoutStatus.AWAITING_ADDRESS,
                    Payout.kind.in_(list(BUYER_BOUND_PAYOUTS)),
                )
            )
        ).scalars()
        for payout in waiting:
            payout.recipient_address = normalized
            payout.status = (
                PayoutStatus.AWAITING_APPROVAL if payout.approvals_required else PayoutStatus.QUEUED
            )
            payout.next_attempt_at = self._clock()
        await self._record(
            deal, Actor.user(user_id), "refund_address_set", deal.status, deal.status, {"address": normalized}
        )
        await self._finish(deal, _Effects())
        return deal

    async def cancel(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        roles = self._roles(deal, actor)
        effects = _Effects()
        if deal.status is DealStatus.AWAITING_PAYMENT and await self.escrow_balance(deal) > 0:
            # Part of the money has already arrived: cancelling means returning it.
            fsm.apply(deal.status, Action.CANCEL, roles, target=DealStatus.REFUND_PENDING)
            await self._refund(
                deal, Action.CANCEL, actor, DealResolution.PARTIAL_PAYMENT_RETURNED, effects, roles=roles
            )
        else:
            await self._transition(deal, Action.CANCEL, actor, roles=roles, target=DealStatus.CANCELLED)
            if deal.status is DealStatus.CANCELLED:
                await self._close_payment_request(deal, PaymentRequestStatus.CANCELLED)
            for other in self._parties_except(deal, user_id):
                effects.notices.append((other, Notice.DEAL_CANCELLED, {}))
        await self._finish(deal, effects)
        return deal

    # ══ payment ═══════════════════════════════════════════════════════════
    async def start_payment(self, user_id: int, deal_id: int) -> PaymentRequest:
        if self._gateway is None:
            raise RuntimeError("payment gateway is not configured")
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        fsm.apply(deal.status, Action.START_PAYMENT, self._roles(deal, actor))
        await get_active_user(self._s, user_id)
        request = await self._gateway.create_request(self._s, deal, now=self._clock())
        deal.payment_expires_at = request.expires_at
        await self._transition(deal, Action.START_PAYMENT, actor, payload={"payment_request_id": request.id})
        effects = _Effects()
        if deal.seller_id is not None:
            effects.notices.append((deal.seller_id, Notice.PAYMENT_STARTED, {}))
        await self._finish(deal, effects)
        return request

    async def extend_payment(self, user_id: int, deal_id: int) -> Deal:
        if self._gateway is None:
            raise RuntimeError("payment gateway is not configured")
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        fsm.apply(deal.status, Action.EXTEND_PAYMENT, self._roles(deal, actor))
        if deal.payment_extended:
            raise LimitExceeded("the payment window can be extended only once")
        request = await self._active_request(deal)
        new_expiry = request.expires_at + self._settings.payment_extension
        if not await self._gateway.extend_request(self._s, request, new_expires_at=new_expiry):
            raise ValidationFailed("this payment method cannot be extended")
        request.expires_at = new_expiry
        deal.payment_expires_at = new_expiry
        deal.payment_extended = True
        await self._record(
            deal,
            actor,
            Action.EXTEND_PAYMENT,
            deal.status,
            deal.status,
            {"expires_at": new_expiry.isoformat()},
        )
        await self._finish(deal, _Effects())
        return deal

    async def fund(self, deal_id: int, *, tx_hash: str | None = None) -> Deal:
        """Called by the payment service after a final deposit made the escrow cover the amount due.

        The deposit itself is already posted to the ledger by the caller; excess is either handed to
        the seller (dust) or returned to the buyer as an overpayment refund.
        """
        deal = await self._lock(deal_id)
        request = await self._active_request(deal)
        escrow = await self.escrow_balance(deal)
        if escrow < request.expected_amount:
            raise InvalidTransition(
                "escrow does not cover the amount due", escrow=escrow, expected=request.expected_amount
            )
        effects = _Effects()
        await self._transition(
            deal,
            Action.FUND,
            Actor.system(),
            tx_hash=tx_hash,
            payload={"received": escrow, "expected": request.expected_amount},
        )
        self._on_funded(deal, request, escrow)
        excess = escrow - request.expected_amount
        if excess > 0:
            await self._handle_overpayment(deal, excess, effects)
        for party in self._parties(deal):
            effects.notices.append((party, Notice.DEAL_FUNDED, {}))
        await self._finish(deal, effects)
        return deal

    async def accept_underpayment(self, user_id: int, deal_id: int) -> Deal:
        """The seller agrees to work for what has actually arrived (exchanges often deduct fees)."""
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        fsm.apply(deal.status, Action.ACCEPT_UNDERPAYMENT, self._roles(deal, actor))
        request = await self._active_request(deal)
        escrow = await self.escrow_balance(deal)
        if not 0 < escrow < request.expected_amount:
            raise InvalidTransition("there is no partial payment to accept", escrow=escrow)
        shortfall = request.expected_amount - escrow
        new_seller_receives = deal.seller_receives - shortfall
        if new_seller_receives < self._spec(deal.network).min_payout:
            raise AmountError("too little has arrived to proceed", escrow=escrow)
        deal.seller_receives = new_seller_receives
        await self._transition(
            deal, Action.ACCEPT_UNDERPAYMENT, actor, payload={"received": escrow, "shortfall": shortfall}
        )
        self._on_funded(deal, request, escrow)
        effects = _Effects()
        if deal.buyer_id is not None:
            effects.notices.append((deal.buyer_id, Notice.DEAL_FUNDED, {"underpayment_accepted": True}))
        await self._finish(deal, effects)
        return deal

    async def on_late_payment(self, deal_id: int, *, tx_hash: str | None = None) -> Deal:
        """A deposit for an expired/cancelled deal became final (already posted to escrow)."""
        deal = await self._lock(deal_id)
        effects = _Effects()
        if deal.status in (DealStatus.EXPIRED, DealStatus.CANCELLED):
            await self._refund(
                deal,
                Action.LATE_PAYMENT,
                Actor.system(),
                DealResolution.LATE_PAYMENT,
                effects,
                tx_hash=tx_hash,
            )
        elif deal.status is DealStatus.REFUND_PENDING:
            escrow = await self.escrow_balance(deal)
            if escrow <= 0:
                raise InvalidTransition("nothing new in escrow")
            await self._record(
                deal,
                Actor.system(),
                "late_payment",
                deal.status,
                deal.status,
                {"escrow": escrow},
                tx_hash=tx_hash,
            )
            await self._refund_payouts(deal, escrow, effects)
        else:
            raise InvalidTransition("late payment handling does not apply", status=deal.status)
        await self._finish(deal, effects)
        return deal

    # ══ execution ═════════════════════════════════════════════════════════
    async def seller_confirm(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        effects = _Effects()
        if deal.status is DealStatus.FUNDED and deal.refund_requested_at is not None:
            # The buyer asked for a refund after the deadline; "I delivered" is an objection.
            await self._open_dispute(
                deal, actor, Action.OBJECT_REFUND, DisputeReason.REFUND_OBJECTION, None, effects
            )
        else:
            await self._transition(deal, Action.SELLER_CONFIRM, actor)
            deal.confirm_wait_started_at = self._clock()
            deal.reminders_sent = 0
            if deal.buyer_id is not None:
                effects.notices.append((deal.buyer_id, Notice.SELLER_CONFIRMED, {}))
        await self._finish(deal, effects)
        return deal

    async def buyer_confirm(self, user_id: int, deal_id: int) -> Deal:
        """Irreversible: releases the escrow to the seller. Only the buyer, only after the seller."""
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        await self._transition(deal, Action.BUYER_CONFIRM, actor)
        await self._s.flush()  # the payouts guard trigger checks the deal status
        escrow = await self.escrow_balance(deal)
        settlement = settle_release(
            escrow, seller_receives=deal.seller_receives, network_fee=deal.network_fee_amount
        )
        await self._post_release(deal, settlement, key=f"release:{deal.id}", kind="buyer_confirmed")
        effects = _Effects()
        await self._create_payout(deal, PayoutKind.SELLER_PAYOUT, settlement.to_seller, effects)
        await self._transition(deal, Action.START_PAYOUT, Actor.system())
        if deal.seller_id is not None:
            effects.notices.append((deal.seller_id, Notice.PAYOUT_STARTED, {"amount": settlement.to_seller}))
        await self._finish(deal, effects)
        log.info("buyer_confirmed", deal_id=deal.id, payout=settlement.to_seller)
        return deal

    async def on_payout_settled(self, deal_id: int) -> Deal:
        """Called by the payout worker after a payout became final; closes the deal when all are."""
        deal = await self._lock(deal_id)
        if deal.status not in (DealStatus.PAYOUT_PENDING, DealStatus.REFUND_PENDING):
            return deal
        open_payouts = await self._s.scalar(
            sa.select(sa.func.count())
            .select_from(Payout)
            .where(
                Payout.deal_id == deal.id,
                Payout.kind != PayoutKind.OVERPAY_REFUND,
                Payout.status.not_in([PayoutStatus.CONFIRMED, PayoutStatus.CANCELLED]),
            )
        )
        if open_payouts:
            return deal
        effects = _Effects()
        await self._complete(deal, effects)
        await self._finish(deal, effects)
        return deal

    # ══ cancellation and refunds after funding ════════════════════════════
    async def request_cancel(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        roles = self._roles(deal, actor)
        role = deal.party_role(user_id)
        fsm.apply(deal.status, Action.REQUEST_CANCEL, roles, target=deal.status)
        now = self._clock()
        if role is PartyRole.BUYER:
            deal.cancel_req_buyer_at = deal.cancel_req_buyer_at or now
        else:
            deal.cancel_req_seller_at = deal.cancel_req_seller_at or now
        effects = _Effects()
        if deal.cancel_req_buyer_at and deal.cancel_req_seller_at:
            await self._refund(
                deal, Action.REQUEST_CANCEL, actor, DealResolution.MUTUAL_CANCEL, effects, roles=roles
            )
        else:
            await self._record(deal, actor, Action.REQUEST_CANCEL, deal.status, deal.status, {})
            for other in self._parties_except(deal, user_id):
                effects.notices.append((other, Notice.CANCEL_REQUESTED, {}))
        await self._finish(deal, effects)
        return deal

    async def revoke_cancel(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        fsm.apply(deal.status, Action.REVOKE_CANCEL, self._roles(deal, actor))
        if deal.party_role(user_id) is PartyRole.BUYER:
            deal.cancel_req_buyer_at = None
        else:
            deal.cancel_req_seller_at = None
        effects = _Effects()
        for other in self._parties_except(deal, user_id):
            effects.notices.append((other, Notice.CANCEL_REVOKED, {}))
        await self._record(deal, actor, Action.REVOKE_CANCEL, deal.status, deal.status, {})
        await self._finish(deal, effects)
        return deal

    async def request_refund(self, user_id: int, deal_id: int) -> Deal:
        """After the execution deadline the buyer may ask for a refund; the seller can object."""
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        fsm.apply(deal.status, Action.REQUEST_REFUND, self._roles(deal, actor))
        if deal.execution_deadline_at is None or self._clock() < deal.execution_deadline_at:
            raise InvalidTransition("the execution deadline has not passed yet")
        if deal.refund_requested_at is not None:
            raise InvalidTransition("a refund has already been requested")
        deal.refund_requested_at = self._clock()
        effects = _Effects()
        if deal.seller_id is not None:
            effects.notices.append(
                (
                    deal.seller_id,
                    Notice.REFUND_REQUESTED,
                    {
                        "objection_window_hours": int(
                            self._settings.refund_objection_window.total_seconds() // 3600
                        ),
                    },
                )
            )
        await self._record(deal, actor, Action.REQUEST_REFUND, deal.status, deal.status, {})
        await self._finish(deal, effects)
        return deal

    async def agree_refund(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        if deal.refund_requested_at is None:
            raise InvalidTransition("no refund was requested")
        effects = _Effects()
        await self._refund(
            deal, Action.AGREE_REFUND, Actor.user(user_id), DealResolution.REFUND_AFTER_DEADLINE, effects
        )
        await self._finish(deal, effects)
        return deal

    async def object_refund(self, user_id: int, deal_id: int, text: str | None = None) -> Deal:
        deal = await self._lock(deal_id)
        if deal.refund_requested_at is None:
            raise InvalidTransition("no refund was requested")
        effects = _Effects()
        await self._open_dispute(
            deal, Actor.user(user_id), Action.OBJECT_REFUND, DisputeReason.REFUND_OBJECTION, text, effects
        )
        await self._finish(deal, effects)
        return deal

    # ══ disputes ══════════════════════════════════════════════════════════
    async def open_dispute(self, user_id: int, deal_id: int, text: str | None = None) -> Deal:
        deal = await self._lock(deal_id)
        effects = _Effects()
        await self._open_dispute(
            deal, Actor.user(user_id), Action.OPEN_DISPUTE, DisputeReason.USER, text, effects
        )
        await self._finish(deal, effects)
        return deal

    async def withdraw_dispute(self, user_id: int, deal_id: int) -> Deal:
        deal = await self._lock(deal_id)
        actor = Actor.user(user_id)
        dispute = await self._open_dispute_row(deal)
        if dispute.reason is DisputeReason.REFUND_OBJECTION:
            raise Forbidden("this dispute can only be decided by an admin")
        opener = dispute.opened_by if dispute.reason is DisputeReason.USER else deal.buyer_id
        if opener != user_id:
            raise Forbidden("only the side that opened the dispute can withdraw it")
        target = deal.status_before_dispute
        if target is None:
            raise InvalidTransition("unknown pre-dispute status")
        await self._transition(deal, Action.WITHDRAW_DISPUTE, actor, target=target)
        dispute.status = DisputeStatus.WITHDRAWN
        dispute.resolved_at = self._clock()
        deal.status_before_dispute = None
        if target is DealStatus.SELLER_CONFIRMED:
            deal.confirm_wait_started_at = self._clock()
            deal.reminders_sent = 0
        effects = _Effects()
        for other in self._parties_except(deal, user_id):
            effects.notices.append((other, Notice.DISPUTE_WITHDRAWN, {}))
        await self._finish(deal, effects)
        return deal

    async def preview_resolution(
        self,
        deal_id: int,
        *,
        decision: DisputeDecision,
        buyer_share_bps: int | None = None,
        keep_fee: bool | None = None,
    ) -> ResolutionPreview:
        deal = await self._get(deal_id)
        return await self._resolution(deal, decision, buyer_share_bps, keep_fee)

    async def resolve_dispute(
        self,
        admin_id: int,
        deal_id: int,
        *,
        decision: DisputeDecision,
        comment: str,
        buyer_share_bps: int | None = None,
        keep_fee: bool | None = None,
    ) -> Deal:
        """A single admin decides the dispute; the decision authorizes the resulting payouts."""
        deal = await self._lock(deal_id)
        admin = await self._admin(admin_id, deal)
        if len(comment.strip()) < COMMENT_MIN_LENGTH:
            raise ValidationFailed("a comment explaining the decision is required", field="comment")
        dispute = await self._open_dispute_row(deal)
        preview = await self._resolution(deal, decision, buyer_share_bps, keep_fee)
        actor = Actor.admin(admin.id)
        action = {
            DisputeDecision.BUYER: Action.RESOLVE_BUYER,
            DisputeDecision.SELLER: Action.RESOLVE_SELLER,
            DisputeDecision.SPLIT: Action.RESOLVE_SPLIT,
        }[decision]
        await self._transition(
            deal,
            action,
            actor,
            roles={Role.ADMIN},
            payload={
                "buyer_share_bps": preview.buyer_share_bps,
                "keep_fee": preview.keep_fee,
                "to_buyer": preview.settlement.to_buyer,
                "to_seller": preview.settlement.to_seller,
            },
        )
        await self._s.flush()
        s = preview.settlement
        await self._post_release(deal, s, key=f"resolve:{deal.id}", kind=f"dispute_{decision.value}")
        effects = _Effects()
        buyer_kind = PayoutKind.BUYER_REFUND if decision is DisputeDecision.BUYER else PayoutKind.SPLIT_BUYER
        seller_kind = (
            PayoutKind.SELLER_PAYOUT if decision is DisputeDecision.SELLER else PayoutKind.SPLIT_SELLER
        )
        if s.to_buyer:
            await self._create_payout(deal, buyer_kind, s.to_buyer, effects, via_decision=True)
        if s.to_seller:
            await self._create_payout(deal, seller_kind, s.to_seller, effects, via_decision=True)
        if decision is DisputeDecision.BUYER:
            await self._transition(deal, Action.START_REFUND, Actor.system())
        else:
            await self._transition(deal, Action.START_PAYOUT, Actor.system())

        dispute.status = DisputeStatus.RESOLVED
        dispute.decision = decision
        dispute.buyer_share_bps = preview.buyer_share_bps
        dispute.keep_fee = preview.keep_fee
        dispute.comment = comment.strip()
        dispute.arbitrator_id = admin.id
        dispute.resolved_at = self._clock()
        deal.resolution = DealResolution(decision.value)
        loser = {DisputeDecision.BUYER: deal.seller_id, DisputeDecision.SELLER: deal.buyer_id}.get(decision)
        if loser is not None:
            await self._s.execute(
                sa.update(User).where(User.id == loser).values(disputes_lost=User.disputes_lost + 1)
            )
        self._s.add(
            AdminAudit(
                admin_id=admin.id,
                action="resolve_dispute",
                target_type="deal",
                target_id=deal.id,
                payload={"decision": decision.value, "buyer_share_bps": preview.buyer_share_bps},
            )
        )
        for party in self._parties(deal):
            effects.notices.append(
                (
                    party,
                    Notice.DISPUTE_RESOLVED,
                    {
                        "decision": decision.value,
                        "to_buyer": s.to_buyer,
                        "to_seller": s.to_seller,
                        "comment": dispute.comment,
                    },
                )
            )
        if not s.to_payable:
            await self._complete(deal, effects)
        await self._finish(deal, effects)
        log.info("dispute_resolved", deal_id=deal.id, decision=decision.value, admin_id=admin.id)
        return deal

    # ══ timers ════════════════════════════════════════════════════════════
    async def process_timer(self, deal_id: int) -> bool:  # noqa: PLR0912 - timer dispatch
        """Run the deal's due timer. Returns False when nothing was due (another worker was faster)."""
        deal = await self._lock(deal_id)
        now = self._clock()
        if deal.next_action_at is None or deal.next_action_at > now:
            return False
        kind = deal.next_action_kind
        effects = _Effects()
        system = Actor.system()
        if kind in (TimerKind.OFFER_EXPIRE, TimerKind.PAY_START_EXPIRE):
            await self._transition(deal, Action.EXPIRE, system, target=DealStatus.EXPIRED)
            for party in self._parties(deal):
                effects.notices.append((party, Notice.DEAL_EXPIRED, {}))
        elif kind is TimerKind.PAYMENT_EXPIRE:
            if await self._has_unconfirmed_transfer(deal):
                deal.next_action_at = now + PAYMENT_EXPIRY_DEFERRAL
                return True
            if await self.escrow_balance(deal) > 0:
                await self._refund(
                    deal, Action.EXPIRE, system, DealResolution.PARTIAL_PAYMENT_RETURNED, effects
                )
            else:
                await self._transition(deal, Action.EXPIRE, system, target=DealStatus.EXPIRED)
                await self._close_payment_request(deal, PaymentRequestStatus.EXPIRED)
                for party in self._parties(deal):
                    effects.notices.append((party, Notice.DEAL_EXPIRED, {}))
        elif kind is TimerKind.EXECUTION_DEADLINE:
            await self._transition(deal, Action.NOTIFY, system, payload={"timer": kind.value})
            deal.deadline_notified_at = now
            if deal.buyer_id is not None:
                effects.notices.append((deal.buyer_id, Notice.DEADLINE_PASSED, {}))
        elif kind is TimerKind.REFUND_WINDOW_ELAPSED:
            await self._refund(
                deal, Action.REFUND_WINDOW_ELAPSED, system, DealResolution.REFUND_AFTER_DEADLINE, effects
            )
        elif kind is TimerKind.BUYER_REMINDER and self._silence_exceeded(deal, now):
            # The worker was behind: do not replay stale reminders, the dispute is due already.
            await self._open_dispute(
                deal, system, Action.OPEN_DISPUTE, DisputeReason.BUYER_TIMEOUT, None, effects
            )
        elif kind is TimerKind.BUYER_REMINDER:
            await self._transition(deal, Action.NOTIFY, system, payload={"timer": kind.value})
            deal.reminders_sent += 1
            if deal.buyer_id is not None:
                effects.notices.append((deal.buyer_id, Notice.BUYER_REMINDER, {"n": deal.reminders_sent}))
        elif kind is TimerKind.AUTO_DISPUTE:
            await self._open_dispute(
                deal, system, Action.OPEN_DISPUTE, DisputeReason.BUYER_TIMEOUT, None, effects
            )
        elif kind is TimerKind.DISPUTE_SLA:
            dispute = await self._open_dispute_row(deal)
            dispute.sla_alerted_at = now
            await self._transition(deal, Action.NOTIFY, system, payload={"timer": kind.value})
            for admin_id in await notifications.admin_ids(self._s):
                effects.notices.append((admin_id, Notice.ADMIN_DISPUTE_SLA, {}))
        await self._finish(deal, effects)
        return True

    # ══ internals: state, audit, timers ═══════════════════════════════════
    async def _get(self, deal_id: int) -> Deal:
        deal = await self._s.get(Deal, deal_id)
        if deal is None:
            raise NotFound("deal not found", deal_id=deal_id)
        return deal

    async def _lock(self, deal_id: int) -> Deal:
        deal = (
            await self._s.execute(
                sa.select(Deal)
                .where(Deal.id == deal_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if deal is None:
            raise NotFound("deal not found", deal_id=deal_id)
        return deal

    def _roles(self, deal: Deal, actor: Actor) -> set[Role]:
        if actor.type is ActorType.SYSTEM:
            return {Role.SYSTEM}
        if actor.type is ActorType.ADMIN:
            return {Role.ADMIN}
        uid = actor.user_id
        roles: set[Role] = set()
        if uid is None:
            return roles
        if uid == deal.buyer_id:
            roles.add(Role.BUYER)
        if uid == deal.seller_id:
            roles.add(Role.SELLER)
        if uid == deal.creator_id:
            roles.add(Role.CREATOR)
        elif (
            uid in (deal.invited_user_id, deal.pending_counterparty_id)
            or deal.counterparty_of(deal.creator_id) == uid
        ):
            roles.add(Role.COUNTERPARTY)
        return roles

    async def _transition(
        self,
        deal: Deal,
        action: Action,
        actor: Actor,
        *,
        target: DealStatus | None = None,
        roles: Iterable[Role] | None = None,
        payload: dict[str, Any] | None = None,
        tx_hash: str | None = None,
    ) -> DealStatus:
        before = deal.status
        after = fsm.apply(
            before, action, roles if roles is not None else self._roles(deal, actor), target=target
        )
        if after is not before:
            self._set_status(deal, after)
        await self._record(deal, actor, action, before, after, payload or {}, tx_hash=tx_hash)
        return after

    def _set_status(self, deal: Deal, status: DealStatus) -> None:
        now = self._clock()
        deal.status = status
        stamp = {
            DealStatus.ACCEPTED: "accepted_at",
            DealStatus.FUNDED: "funded_at",
            DealStatus.SELLER_CONFIRMED: "seller_confirmed_at",
            DealStatus.BUYER_CONFIRMED: "buyer_confirmed_at",
        }.get(status)
        if stamp is not None and getattr(deal, stamp) is None:
            setattr(deal, stamp, now)
        if status in (DealStatus.COMPLETED, DealStatus.REFUNDED, DealStatus.CANCELLED, DealStatus.EXPIRED):
            deal.closed_at = now

    async def _record(  # noqa: PLR0917 - (deal, actor, action, from, to, payload) reads naturally
        self,
        deal: Deal,
        actor: Actor,
        action: str,
        before: DealStatus | None,
        after: DealStatus | None,
        payload: dict[str, Any],
        *,
        tx_hash: str | None = None,
    ) -> None:
        """Append a hash-chained audit event (who, when, from → to, tx hash, details)."""
        if deal.id is None:
            await self._s.flush()
        seq = deal.event_seq + 1
        created_at = self._clock()
        fields = {
            "deal_id": deal.id,
            "seq": seq,
            "actor_type": actor.type.value,
            "actor_id": actor.user_id,
            "action": str(action),
            "from": before.value if before else None,
            "to": after.value if after else None,
            "tx_hash": tx_hash,
            "payload": payload,
            "at": created_at.isoformat(),
        }
        digest = hashlib.sha256((deal.last_event_hash or b"\x00" * 32) + _canonical(fields)).digest()
        self._s.add(
            DealEvent(
                deal_id=deal.id,
                seq=seq,
                actor_type=actor.type,
                actor_id=actor.user_id,
                action=str(action),
                from_status=before,
                to_status=after,
                tx_hash=tx_hash,
                payload=json.loads(_canonical(payload)),
                prev_hash=deal.last_event_hash,
                hash=digest,
                created_at=created_at,
            )
        )
        deal.event_seq = seq
        deal.last_event_hash = digest

    async def _finish(self, deal: Deal, effects: _Effects) -> None:
        dispute = (
            await self._open_dispute_row(deal, required=False) if deal.status is DealStatus.DISPUTED else None
        )
        timer = next_timer(deal, self._settings, dispute)
        deal.next_action_at = timer.at if timer else None
        deal.next_action_kind = timer.kind if timer else None
        for user_id, notice, payload in effects.notices:
            await notifications.enqueue(
                self._s,
                user_id=user_id,
                kind=notice,
                deal_id=deal.id,
                dedupe_key=f"d{deal.id}:e{deal.event_seq}:{notice.value}:{user_id}",
                payload={"status": deal.status.value, "code": deal.code, **payload},
                now=self._clock(),
            )
        await self._s.flush()

    # ══ internals: money ══════════════════════════════════════════════════
    def _asset(self, deal: Deal) -> str:
        return asset_code(deal.asset, deal.network)

    def _spec(self, network: Network) -> AssetSpec:
        try:
            return self._chains.spec(network)
        except KeyError as exc:
            raise ValidationFailed("this network is not available", network=network) from exc

    def _apply_breakdown(self, deal: Deal, b: FeeBreakdown, fee_payer: FeePayer) -> None:
        deal.amount = b.amount
        deal.fee_payer = fee_payer
        deal.fee_amount = b.fee
        deal.network_fee_amount = b.network_fee
        deal.buyer_pays = b.buyer_pays
        deal.seller_receives = b.seller_receives

    def _on_funded(self, deal: Deal, request: PaymentRequest, escrow: int) -> None:
        now = self._clock()
        deal.received_amount = escrow
        deal.execution_deadline_at = now + timedelta(hours=deal.execution_hours)
        request.status = PaymentRequestStatus.PAID
        if request.tag_reserved:
            # Keep the tag reserved for a while: a duplicate payment of the same amount still
            # matches this deal (and is refunded) instead of funding someone else's.
            request.tag_release_at = now + self._settings.tag_cooldown

    async def _handle_overpayment(self, deal: Deal, excess: int, effects: _Effects) -> None:
        spec = self._spec(deal.network)
        dust = to_minor(self._settings.overpay_dust_usdt, spec.decimals)
        if excess <= dust or excess - deal.network_fee_amount < spec.min_payout:
            # Not worth a refund transaction: goes to the seller with the payout (buyer is told).
            deal.seller_receives += excess
            if deal.buyer_id is not None:
                effects.notices.append((deal.buyer_id, Notice.DEAL_FUNDED, {"overpaid_to_seller": excess}))
            await self._record(
                deal, Actor.system(), "overpayment_to_seller", deal.status, deal.status, {"excess": excess}
            )
            return
        refund = settle_refund(excess, network_fee=deal.network_fee_amount, min_payout=spec.min_payout)
        await self._post_release(deal, refund, key=f"overpay:{deal.id}:{deal.event_seq}", kind="overpayment")
        await self._create_payout(deal, PayoutKind.OVERPAY_REFUND, refund.to_buyer, effects)
        await self._record(
            deal, Actor.system(), "overpayment_refund", deal.status, deal.status, {"excess": excess}
        )

    async def _post_release(self, deal: Deal, s: Settlement, *, key: str, kind: str) -> None:
        await self._ledger.post(
            idempotency_key=key,
            kind=kind,
            deal_id=deal.id,
            postings=postings.release_escrow(
                asset=self._asset(deal),
                deal_id=deal.id,
                to_payable=s.to_payable,
                fee_revenue=s.fee_revenue,
                network_fee_recovered=s.network_fee_recovered,
            ),
        )

    async def _refund(
        self,
        deal: Deal,
        action: Action,
        actor: Actor,
        resolution: DealResolution,
        effects: _Effects,
        *,
        roles: Iterable[Role] | None = None,
        tx_hash: str | None = None,
    ) -> None:
        """Return the whole escrow to the buyer (minus the refund network fee and, if configured, the fee)."""
        escrow = await self.escrow_balance(deal)
        if escrow <= 0:
            raise InvalidTransition("there is nothing to refund")
        await self._transition(
            deal,
            action,
            actor,
            target=DealStatus.REFUND_PENDING,
            roles=roles,
            tx_hash=tx_hash,
            payload={"resolution": resolution.value, "escrow": escrow},
        )
        deal.resolution = resolution
        deal.cancel_req_buyer_at = deal.cancel_req_seller_at = None
        await self._close_payment_request(
            deal, PaymentRequestStatus.EXPIRED if action is Action.EXPIRE else PaymentRequestStatus.CANCELLED
        )
        await self._refund_payouts(deal, escrow, effects)
        for party in self._parties_except(deal, deal.buyer_id):
            effects.notices.append((party, Notice.REFUND_STARTED, {}))

    async def _refund_payouts(self, deal: Deal, escrow: int, effects: _Effects) -> None:
        spec = self._spec(deal.network)
        service_fee = min(deal.fee_amount, escrow) if self._settings.refund_charges_service_fee else 0
        s = settle_refund(
            escrow, network_fee=deal.network_fee_amount, min_payout=spec.min_payout, service_fee=service_fee
        )
        await self._post_release(deal, s, key=f"refund:{deal.id}:{deal.event_seq}", kind="refund")
        if s.to_buyer:
            await self._create_payout(deal, PayoutKind.BUYER_REFUND, s.to_buyer, effects)
        else:
            await self._complete(deal, effects)
        if deal.buyer_id is not None:
            effects.notices.append((deal.buyer_id, Notice.REFUND_STARTED, {"amount": s.to_buyer}))

    async def _create_payout(
        self, deal: Deal, kind: PayoutKind, amount: int, effects: _Effects, *, via_decision: bool = False
    ) -> Payout:
        spec = self._spec(deal.network)
        seq = 1 + (
            await self._s.scalar(
                sa.select(sa.func.count())
                .select_from(Payout)
                .where(Payout.deal_id == deal.id, Payout.kind == kind)
            )
            or 0
        )
        to_buyer = kind in BUYER_BOUND_PAYOUTS
        recipient_user = deal.buyer_id if to_buyer else deal.seller_id
        if deal.provider is Provider.CRYPTOBOT:
            recipient_user_id, recipient_address = recipient_user, None
        else:
            recipient_user_id = None
            recipient_address = deal.buyer_refund_address if to_buyer else deal.seller_payout_address
            if not to_buyer and not recipient_address:
                raise ValidationFailed("seller payout address is missing")
        threshold = to_minor(self._settings.manual_approval_threshold_usdt, spec.decimals)
        needs_approval = amount >= threshold and (
            not via_decision or self._settings.dispute_payout_requires_approval
        )
        approvals = self._settings.large_payout_approvals if needs_approval else 0
        if recipient_user_id is None and recipient_address is None:
            status = PayoutStatus.AWAITING_ADDRESS
        elif approvals:
            status = PayoutStatus.AWAITING_APPROVAL
        else:
            status = PayoutStatus.QUEUED
        payout = Payout(
            idempotency_key=f"{kind.value}:{deal.code}:{seq}",
            deal_id=deal.id,
            kind=kind,
            provider=deal.provider,
            network=deal.network,
            asset=deal.asset,
            recipient_user_id=recipient_user_id,
            recipient_address=recipient_address,
            amount=amount,
            status=status,
            approvals_required=approvals,
            next_attempt_at=self._clock() if status is PayoutStatus.QUEUED else None,
        )
        self._s.add(payout)
        await self._s.flush()
        if status is PayoutStatus.AWAITING_ADDRESS and deal.buyer_id is not None:
            effects.notices.append((deal.buyer_id, Notice.REFUND_ADDRESS_NEEDED, {"payout_id": payout.id}))
        if status is PayoutStatus.AWAITING_APPROVAL:
            for admin_id in await notifications.admin_ids(self._s):
                effects.notices.append((admin_id, Notice.ADMIN_PAYOUT_APPROVAL, {"payout_id": payout.id}))
        return payout

    async def _complete(self, deal: Deal, effects: _Effects) -> None:
        if deal.status is DealStatus.REFUND_PENDING:
            await self._transition(deal, Action.COMPLETE_REFUND, Actor.system())
            for party in self._parties(deal):
                effects.notices.append((party, Notice.DEAL_REFUNDED, {}))
            return
        await self._transition(deal, Action.COMPLETE, Actor.system())
        volume = rescale(deal.amount, deal.decimals, USDT_VOLUME_DECIMALS)
        await self._s.execute(
            sa.update(User)
            .where(User.id.in_(list(self._parties(deal))))
            .values(deals_completed=User.deals_completed + 1, volume_usdt=User.volume_usdt + volume)
        )
        for party in self._parties(deal):
            effects.notices.append((party, Notice.DEAL_COMPLETED, {}))

    async def _resolution(
        self, deal: Deal, decision: DisputeDecision, buyer_share_bps: int | None, keep_fee: bool | None
    ) -> ResolutionPreview:
        if deal.status is not DealStatus.DISPUTED:
            raise InvalidTransition("deal is not in dispute", status=deal.status)
        if decision is DisputeDecision.SPLIT:
            if buyer_share_bps is None or not 0 < buyer_share_bps < BPS:
                raise ValidationFailed(
                    "split needs a buyer share between 0 and 100 %", field="buyer_share_bps"
                )
            bps = buyer_share_bps
        else:
            bps = BPS if decision is DisputeDecision.BUYER else 0
        keep = self._settings.dispute_keeps_fee_default if keep_fee is None else keep_fee
        escrow = await self.escrow_balance(deal)
        spec = self._spec(deal.network)
        fee_part = 0
        if keep:
            try:
                fee_part = service_fee_in_escrow(escrow, deal.seller_receives, deal.network_fee_amount)
            except AmountError:
                fee_part = min(deal.fee_amount, escrow)
        settlement = settle_dispute(
            escrow,
            buyer_share_bps=bps,
            fee_part=fee_part,
            network_fee=deal.network_fee_amount,
            min_payout=spec.min_payout,
        )
        return ResolutionPreview(decision, bps, keep, escrow, settlement)

    # ══ internals: disputes ═══════════════════════════════════════════════
    async def _open_dispute(  # noqa: PLR0917
        self,
        deal: Deal,
        actor: Actor,
        action: Action,
        reason: DisputeReason,
        text: str | None,
        effects: _Effects,
    ) -> None:
        before = deal.status
        await self._transition(
            deal, action, actor, target=DealStatus.DISPUTED, payload={"reason": reason.value}
        )
        deal.status_before_dispute = before
        deal.cancel_req_buyer_at = deal.cancel_req_seller_at = None
        deal.refund_requested_at = None
        dispute = Dispute(
            deal_id=deal.id,
            opened_by=actor.user_id,
            reason=reason,
            reason_text=(text or "").strip()[:2000] or None,
            created_at=self._clock(),
        )
        self._s.add(dispute)
        await self._s.flush()
        await self._s.execute(
            sa.update(User)
            .where(User.id.in_(list(self._parties(deal))))
            .values(deals_disputed=User.deals_disputed + 1)
        )
        for party in self._parties_except(deal, actor.user_id):
            effects.notices.append((party, Notice.DISPUTE_OPENED, {"reason": reason.value}))
        for admin_id in await notifications.admin_ids(self._s):
            if admin_id not in self._parties(deal):
                effects.notices.append((admin_id, Notice.ADMIN_DISPUTE_OPENED, {"reason": reason.value}))

    async def _open_dispute_row(self, deal: Deal, *, required: bool = True) -> Dispute:
        dispute = (
            await self._s.execute(
                sa.select(Dispute).where(
                    Dispute.deal_id == deal.id,
                    Dispute.status.in_([DisputeStatus.OPEN, DisputeStatus.IN_REVIEW]),
                )
            )
        ).scalar_one_or_none()
        if dispute is None and required:
            raise InvalidTransition("there is no open dispute")
        return dispute  # type: ignore[return-value]

    async def _admin(self, admin_id: int, deal: Deal) -> User:
        admin = await self._s.get(User, admin_id)
        if admin is None or admin.is_banned or admin.role not in (UserRole.ADMIN, UserRole.ARBITRATOR):
            raise Forbidden("only an admin can decide disputes")
        if admin_id in (deal.buyer_id, deal.seller_id, deal.creator_id):
            raise Forbidden("an admin cannot decide their own deal")
        return admin

    # ══ internals: validation ═════════════════════════════════════════════
    def _check_version(self, deal: Deal, version: int) -> None:
        if deal.version != version:
            raise VersionConflict("the deal terms have changed", current=deal.version, seen=version)

    def _validate_terms(self, terms: str) -> str:
        cleaned = terms.strip()
        if not TERMS_MIN_LENGTH <= len(cleaned) <= self._settings.terms_max_length:
            raise ValidationFailed(
                "terms length is out of range",
                field="terms",
                min=TERMS_MIN_LENGTH,
                max=self._settings.terms_max_length,
            )
        return cleaned

    def _validate_hours(self, hours: int) -> int:
        if not 1 <= hours <= self._settings.execution_hours_max:
            raise ValidationFailed(
                "execution time is out of range",
                field="execution_hours",
                max=self._settings.execution_hours_max,
            )
        return hours

    def _validate_amount(self, spec: AssetSpec, amount: Decimal, user: User) -> int:
        minor = spec.minor(amount)
        low = spec.minor(self._settings.deal_min_usdt)
        high = spec.minor(self._limit_for(user))
        if minor < low:
            raise LimitExceeded("amount below minimum", limit=str(self._settings.deal_min_usdt), kind="min")
        if minor > high:
            raise LimitExceeded("amount above the limit", limit=str(self._limit_for(user)), kind="max")
        return minor

    def _limit_for(self, user: User) -> Decimal:
        if user.deals_completed < self._settings.new_user_completed_deals:
            return self._settings.new_user_deal_max_usdt
        return self._settings.deal_max_usdt

    def _check_amount_limit(self, user: User, deal: Deal) -> None:
        if deal.amount > to_minor(self._limit_for(user), deal.decimals):
            raise LimitExceeded(
                "amount above the counterparty's limit",
                limit=str(self._limit_for(user)),
                kind="counterparty_max",
            )

    def _check_invitee(self, deal: Deal, user: User) -> bool:
        """Is this user allowed to take the counterparty seat? Returns True for an open invite."""
        role = deal.creator_role.other
        bound = deal.buyer_id if role is PartyRole.BUYER else deal.seller_id
        if bound is not None:
            if bound != user.id:
                raise Forbidden("the deal belongs to another counterparty")
            return False
        if deal.invited_user_id is not None:
            if deal.invited_user_id != user.id:
                raise Forbidden("the deal was offered to another user")
            return False
        if deal.invited_username is not None:
            if not user.username or user.username.lower() != deal.invited_username.lower():
                raise Forbidden("the deal was offered to another user")
            return False
        if deal.pending_counterparty_id not in (None, user.id):
            raise Forbidden("another user has already taken this deal")
        return True

    def _bind_counterparty(self, deal: Deal, user_id: int) -> None:
        if deal.creator_role is PartyRole.BUYER:
            deal.seller_id = user_id
        else:
            deal.buyer_id = user_id

    async def _resolve_counterparty(self, cmd: NewDeal) -> tuple[int | None, str | None]:
        if cmd.counterparty_user_id is not None:
            if cmd.counterparty_user_id == cmd.creator_id:
                raise ValidationFailed("you cannot make a deal with yourself", field="counterparty")
            if await is_blacklisted(self._s, user_id=cmd.counterparty_user_id):
                raise Forbidden("counterparty is blocked")
            return cmd.counterparty_user_id, None
        if cmd.counterparty_username:
            name = cmd.counterparty_username.strip().lstrip("@")
            if not name:
                return None, None
            known = await find_by_username(self._s, name)
            if known is not None:
                if known.id == cmd.creator_id:
                    raise ValidationFailed("you cannot make a deal with yourself", field="counterparty")
                if known.is_banned:
                    raise Forbidden("counterparty is blocked")
                return known.id, None
            return None, name
        return None, None

    async def _validate_payout_address(self, network: Network, address: str) -> str:
        normalized = validate_address(network, address, allow_testnet=self._settings.testnet_mode)
        if await self._is_own_address(network, normalized):
            # Paying out to our own deposit address would be credited as a new deposit.
            raise ValidationFailed("this is the bot's own address", field="address")
        if await is_blacklisted(self._s, address=normalized):
            raise Forbidden("address is blocked")
        return normalized

    async def _is_own_address(self, network: Network, address: str) -> bool:
        configured = self._chains.networks.get(network)
        if (
            configured
            and configured.deposit_address
            and same_address(network, configured.deposit_address, address)
        ):
            return True
        lowered = address.lower()
        for model, column in ((HotWallet, HotWallet.address), (DepositAddress, DepositAddress.address)):
            found = await self._s.scalar(
                sa.select(sa.literal(True))
                .select_from(model)
                .where(sa.func.lower(column) == lowered)
                .limit(1)
            )
            if found:
                return True
        return False

    # ══ internals: misc ═══════════════════════════════════════════════════
    async def _insert_with_unique_code(self, deal: Deal) -> None:
        for _ in range(5):
            deal.code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
            try:
                async with self._s.begin_nested():
                    self._s.add(deal)
                    await self._s.flush()
                return
            except IntegrityError as exc:
                if "uq_deals_code" not in str(exc.orig):
                    raise
                self._s.expunge(deal)
        raise DomainError("could not allocate a deal code")

    async def _active_request(self, deal: Deal) -> PaymentRequest:
        request = (
            await self._s.execute(
                sa.select(PaymentRequest)
                .where(
                    PaymentRequest.deal_id == deal.id, PaymentRequest.status == PaymentRequestStatus.ACTIVE
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if request is None:
            raise InvalidTransition("there is no active payment request")
        return request

    async def _close_payment_request(self, deal: Deal, status: PaymentRequestStatus) -> None:
        request = (
            await self._s.execute(
                sa.select(PaymentRequest)
                .where(
                    PaymentRequest.deal_id == deal.id, PaymentRequest.status == PaymentRequestStatus.ACTIVE
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if request is None:
            return
        request.status = status
        if request.tag_reserved:
            request.tag_release_at = self._clock() + self._settings.tag_cooldown
        if status is PaymentRequestStatus.CANCELLED and self._gateway is not None:
            await self._gateway.cancel_request(self._s, request)

    async def _has_unconfirmed_transfer(self, deal: Deal) -> bool:
        found = await self._s.scalar(
            sa.select(sa.literal(True))
            .select_from(IncomingTransfer)
            .where(IncomingTransfer.deal_id == deal.id, IncomingTransfer.status == TransferStatus.SEEN)
            .limit(1)
        )
        return bool(found)

    def _inline_card_notice(self, deal: Deal, effects: _Effects) -> None:
        """The card posted via inline mode in some chat now says the deal was accepted."""
        if deal.inline_message_id:
            effects.notices.append(
                (deal.creator_id, Notice.INLINE_CARD, {"inline_message_id": deal.inline_message_id})
            )

    def _silence_exceeded(self, deal: Deal, now: datetime) -> bool:
        started = deal.confirm_wait_started_at
        return started is not None and now >= started + self._settings.buyer_silence_dispute_after

    def _parties(self, deal: Deal) -> list[int]:
        return [uid for uid in (deal.buyer_id, deal.seller_id) if uid is not None]

    def _parties_except(self, deal: Deal, user_id: int | None) -> list[int]:
        return [uid for uid in self._parties(deal) if uid != user_id]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()
