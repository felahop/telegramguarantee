"""Admin operations: dispute queue and dossier, payout approvals, unidentified payments, balances,
search, bans. Every action is written to ``admin_audit``.

Money-moving operations (dispute decision, payout approval, assigning/returning an unidentified
payment) are gated by TOTP in the bot layer; the rules themselves (who may do what, ledger
balance, deal status) are enforced here and in the database.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chains.addresses import validate_address
from app.config import ChainsConfig, Settings
from app.db.models import (
    AdminAudit,
    Deal,
    DealEvent,
    DealMessage,
    Dispute,
    IncomingTransfer,
    LedgerAccount,
    PaymentRequest,
    Payout,
    PayoutApproval,
    User,
)
from app.domain.enums import (
    OPEN_DISPUTE_STATUSES,
    ApprovalDecision,
    Asset,
    DealStatus,
    DisputeStatus,
    MatchMethod,
    MatchStatus,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
    UserRole,
)
from app.domain.errors import Forbidden, InvalidTransition, NotFound, ValidationFailed
from app.domain.money import asset_code
from app.services import ledger as postings
from app.services import notifications
from app.services.deals import DealService, PaymentGateway, utcnow
from app.services.ledger import AccountCode, AccountRef, Ledger
from app.services.notifications import Notice

ADMINS = frozenset({UserRole.ADMIN})
ARBITERS = frozenset({UserRole.ADMIN, UserRole.ARBITRATOR})
CANDIDATE_WINDOW = timedelta(days=7)
_CODE_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{8}$")
_DEBIT_NORMAL = {"asset", "expense"}


@dataclass
class AdminCounts:
    disputes: int
    approvals: int
    unidentified: int
    failed_payouts: int


@dataclass
class DisputeDossier:
    deal: Deal
    dispute: Dispute
    users: dict[int, User]
    events: list[DealEvent]
    transfers: list[IncomingTransfer]
    payouts: list[Payout]
    messages: list[DealMessage]
    escrow: int


@dataclass
class BalanceRow:
    asset: str
    custody: dict[str, int] = field(default_factory=dict)  # wallet → balance
    by_code: dict[str, int] = field(default_factory=dict)  # account code → normal balance

    def get(self, code: AccountCode) -> int:
        return self.by_code.get(code.value, 0)


@dataclass
class SearchResult:
    deals: list[Deal] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    transfers: list[IncomingTransfer] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.deals or self.users or self.transfers)


class AdminService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        chains: ChainsConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
        gateway: PaymentGateway | None = None,
    ) -> None:
        self._s = session
        self._settings = settings
        self._chains = chains
        self._clock = clock
        self._gateway = gateway
        self._ledger = Ledger(session)

    @property
    def deals(self) -> DealService:
        return DealService(self._s, self._settings, self._chains, gateway=self._gateway, clock=self._clock)

    # ── access ─────────────────────────────────────────────────────────────
    async def require(self, user_id: int, roles: frozenset[UserRole] = ADMINS) -> User:
        user = await self._s.get(User, user_id)
        if user is None or user.is_banned or user.role not in roles:
            raise Forbidden("admin rights required")
        return user

    def _audit(
        self, admin_id: int, action: str, target_type: str, target_id: int | None, **payload: Any
    ) -> None:
        self._s.add(
            AdminAudit(
                admin_id=admin_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                created_at=self._clock(),
            )
        )

    async def counts(self) -> AdminCounts:
        async def count(stmt: sa.Select[Any]) -> int:
            return int(await self._s.scalar(stmt) or 0)

        return AdminCounts(
            disputes=await count(
                sa.select(sa.func.count())
                .select_from(Dispute)
                .where(Dispute.status.in_(list(OPEN_DISPUTE_STATUSES)))
            ),
            approvals=await count(
                sa.select(sa.func.count())
                .select_from(Payout)
                .where(Payout.status == PayoutStatus.AWAITING_APPROVAL)
            ),
            unidentified=await count(
                sa.select(sa.func.count())
                .select_from(IncomingTransfer)
                .where(IncomingTransfer.match_status == MatchStatus.UNIDENTIFIED)
            ),
            failed_payouts=await count(
                sa.select(sa.func.count())
                .select_from(Payout)
                .where(Payout.status.in_([PayoutStatus.FAILED_RETRYABLE, PayoutStatus.FAILED_FINAL]))
            ),
        )

    # ── disputes ───────────────────────────────────────────────────────────
    async def open_disputes(self, *, offset: int = 0, limit: int = 10) -> list[tuple[Dispute, Deal]]:
        rows = await self._s.execute(
            sa.select(Dispute, Deal)
            .join(Deal, Deal.id == Dispute.deal_id)
            .where(Dispute.status.in_(list(OPEN_DISPUTE_STATUSES)))
            .order_by(Dispute.created_at)
            .offset(offset)
            .limit(limit)
        )
        return [(d, deal) for d, deal in rows.all()]

    async def dossier(self, deal_id: int, *, events: int = 15, messages: int = 30) -> DisputeDossier:
        deal = await self._s.get(Deal, deal_id)
        if deal is None:
            raise NotFound("deal not found")
        dispute = (
            await self._s.execute(
                sa.select(Dispute).where(Dispute.deal_id == deal_id).order_by(Dispute.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if dispute is None:
            raise NotFound("no dispute for this deal")
        ids = [deal.buyer_id, deal.seller_id, dispute.arbitrator_id]
        users = {
            u.id: u
            for u in (
                await self._s.execute(sa.select(User).where(User.id.in_([i for i in ids if i is not None])))
            ).scalars()
        }
        event_rows = list(
            (
                await self._s.execute(
                    sa.select(DealEvent)
                    .where(DealEvent.deal_id == deal_id)
                    .order_by(DealEvent.seq.desc())
                    .limit(events)
                )
            ).scalars()
        )
        transfers = list(
            (
                await self._s.execute(
                    sa.select(IncomingTransfer)
                    .where(IncomingTransfer.deal_id == deal_id)
                    .order_by(IncomingTransfer.id)
                )
            ).scalars()
        )
        payouts = list(
            (
                await self._s.execute(sa.select(Payout).where(Payout.deal_id == deal_id).order_by(Payout.id))
            ).scalars()
        )
        message_rows = list(
            (
                await self._s.execute(
                    sa.select(DealMessage)
                    .where(DealMessage.deal_id == deal_id)
                    .order_by(DealMessage.id.desc())
                    .limit(messages)
                )
            ).scalars()
        )
        escrow = await self._ledger.normal_balance(
            AccountRef.escrow(asset_code(deal.asset, deal.network), deal.id)
        )
        return DisputeDossier(
            deal,
            dispute,
            users,
            list(reversed(event_rows)),
            transfers,
            payouts,
            list(reversed(message_rows)),
            escrow,
        )

    async def take_dispute(self, admin_id: int, deal_id: int) -> Dispute:
        admin = await self.require(admin_id, ARBITERS)
        deal = await self.deals.get_for_invitee(admin.id, deal_id)
        if admin.id in (deal.buyer_id, deal.seller_id):
            raise Forbidden("an admin cannot handle their own deal")
        dispute = (
            await self._s.execute(
                sa.select(Dispute)
                .where(Dispute.deal_id == deal_id, Dispute.status.in_(list(OPEN_DISPUTE_STATUSES)))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if dispute is None:
            raise InvalidTransition("there is no open dispute")
        dispute.status = DisputeStatus.IN_REVIEW
        dispute.arbitrator_id = admin.id
        self._audit(admin.id, "take_dispute", "deal", deal_id)
        for party in (deal.buyer_id, deal.seller_id):
            if party is not None:
                await notifications.enqueue(
                    self._s,
                    user_id=party,
                    kind=Notice.DISPUTE_IN_REVIEW,
                    deal_id=deal.id,
                    dedupe_key=f"in-review:{dispute.id}:{party}",
                    now=self._clock(),
                    payload={"status": deal.status.value, "code": deal.code},
                )
        await self._s.flush()
        return dispute

    async def log_arbiter_message(self, admin_id: int, deal_id: int, text: str) -> Deal:
        admin = await self.require(admin_id, ARBITERS)
        deal = await self._s.get(Deal, deal_id)
        if deal is None:
            raise NotFound("deal not found")
        if admin.id in (deal.buyer_id, deal.seller_id):
            raise Forbidden("an admin cannot handle their own deal")
        self._s.add(
            DealMessage(
                deal_id=deal.id,
                sender_id=admin.id,
                sender_role="arbitrator",
                text=text,
                flagged=[],
                created_at=self._clock(),
            )
        )
        self._audit(admin.id, "arbiter_message", "deal", deal.id)
        await self._s.flush()
        return deal

    # ── payouts ────────────────────────────────────────────────────────────
    async def payouts_in(
        self, statuses: list[PayoutStatus], *, offset: int = 0, limit: int = 10
    ) -> list[Payout]:
        rows = await self._s.execute(
            sa.select(Payout)
            .where(Payout.status.in_(statuses))
            .order_by(Payout.id)
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars())

    async def _lock_payout(self, payout_id: int) -> Payout:
        payout = (
            await self._s.execute(sa.select(Payout).where(Payout.id == payout_id).with_for_update())
        ).scalar_one_or_none()
        if payout is None:
            raise NotFound("payout not found")
        return payout

    async def _check_not_party(self, admin_id: int, payout: Payout) -> None:
        if payout.deal_id is None:
            return
        deal = await self._s.get(Deal, payout.deal_id)
        if deal is not None and admin_id in (deal.buyer_id, deal.seller_id, deal.creator_id):
            raise Forbidden("an admin cannot approve a payout of their own deal")

    async def approve_payout(self, admin_id: int, payout_id: int) -> Payout:
        admin = await self.require(admin_id)
        payout = await self._lock_payout(payout_id)
        if payout.status is not PayoutStatus.AWAITING_APPROVAL:
            raise InvalidTransition("the payout is not waiting for approval", status=payout.status)
        await self._check_not_party(admin.id, payout)
        await self._record_approval(payout, admin.id, ApprovalDecision.APPROVE)
        approvals = int(
            await self._s.scalar(
                sa.select(sa.func.count())
                .select_from(PayoutApproval)
                .where(
                    PayoutApproval.payout_id == payout.id, PayoutApproval.decision == ApprovalDecision.APPROVE
                )
            )
            or 0
        )
        if approvals >= max(payout.approvals_required, 1):
            payout.status = PayoutStatus.QUEUED
            payout.next_attempt_at = self._clock()
            payout.last_error = None
        self._audit(admin.id, "approve_payout", "payout", payout.id, approvals=approvals)
        await self._s.flush()
        return payout

    async def reject_payout(self, admin_id: int, payout_id: int, reason: str) -> Payout:
        """The payout stays on hold (money remains owed); the reason is visible to other admins."""
        admin = await self.require(admin_id)
        payout = await self._lock_payout(payout_id)
        if payout.status is not PayoutStatus.AWAITING_APPROVAL:
            raise InvalidTransition("the payout is not waiting for approval", status=payout.status)
        await self._check_not_party(admin.id, payout)
        await self._record_approval(payout, admin.id, ApprovalDecision.REJECT)
        payout.last_error = f"held by admin {admin.id}: {reason.strip()[:300]}"
        self._audit(admin.id, "reject_payout", "payout", payout.id, reason=reason[:300])
        await self._s.flush()
        return payout

    async def _record_approval(self, payout: Payout, admin_id: int, decision: ApprovalDecision) -> None:
        await self._s.execute(
            pg_insert(PayoutApproval)
            .values(
                payout_id=payout.id,
                admin_id=admin_id,
                decision=decision,
                totp_ok=True,
                created_at=self._clock(),
            )
            .on_conflict_do_update(
                index_elements=[PayoutApproval.payout_id, PayoutApproval.admin_id],
                set_={"decision": decision, "created_at": self._clock()},
            )
        )

    async def retry_payout(self, admin_id: int, payout_id: int) -> Payout:
        admin = await self.require(admin_id)
        payout = await self._lock_payout(payout_id)
        if payout.status not in (PayoutStatus.FAILED_RETRYABLE, PayoutStatus.FAILED_FINAL):
            raise InvalidTransition("only failed payouts can be retried", status=payout.status)
        payout.status = PayoutStatus.QUEUED
        payout.next_attempt_at = self._clock()
        payout.attempts = 0
        self._audit(admin.id, "retry_payout", "payout", payout.id, previous_error=payout.last_error or "")
        await self._s.flush()
        return payout

    # ── unidentified payments ──────────────────────────────────────────────
    async def unidentified(self, *, offset: int = 0, limit: int = 10) -> list[IncomingTransfer]:
        rows = await self._s.execute(
            sa.select(IncomingTransfer)
            .where(IncomingTransfer.match_status == MatchStatus.UNIDENTIFIED)
            .order_by(IncomingTransfer.id)
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars())

    async def candidates(self, transfer: IncomingTransfer, *, limit: int = 5) -> list[Deal]:
        """Deals this money most likely belongs to: same network, waiting or recently closed, nearest
        amount first."""
        since = self._clock() - CANDIDATE_WINDOW
        rows = await self._s.execute(
            sa.select(Deal)
            .where(
                Deal.network == transfer.network,
                Deal.status.in_([DealStatus.AWAITING_PAYMENT, DealStatus.EXPIRED, DealStatus.CANCELLED]),
                Deal.created_at >= since,
            )
            .order_by(sa.func.abs(Deal.buyer_pays - transfer.amount), Deal.id.desc())
            .limit(limit)
        )
        return list(rows.scalars())

    async def _lock_unidentified(self, transfer_id: int) -> IncomingTransfer:
        transfer = (
            await self._s.execute(
                sa.select(IncomingTransfer).where(IncomingTransfer.id == transfer_id).with_for_update()
            )
        ).scalar_one_or_none()
        if transfer is None:
            raise NotFound("payment not found")
        if transfer.match_status is not MatchStatus.UNIDENTIFIED:
            raise InvalidTransition("this payment has already been handled")
        return transfer

    async def deal_by_code(self, code: str) -> Deal:
        deal = (
            await self._s.execute(sa.select(Deal).where(Deal.code == code.strip().upper().lstrip("#")))
        ).scalar_one_or_none()
        if deal is None:
            raise NotFound("deal not found")
        return deal

    async def assign_transfer(self, admin_id: int, transfer_id: int, deal_code: str) -> Deal:
        admin = await self.require(admin_id)
        transfer = await self._lock_unidentified(transfer_id)
        deal = await self.deal_by_code(deal_code)
        if admin.id in (deal.buyer_id, deal.seller_id, deal.creator_id):
            raise Forbidden("an admin cannot handle their own deal")
        if deal.network is not transfer.network:
            raise ValidationFailed("the payment came in another network than the deal's", field="network")
        accepting = deal.status in (
            DealStatus.AWAITING_PAYMENT,
            DealStatus.EXPIRED,
            DealStatus.CANCELLED,
            DealStatus.REFUND_PENDING,
        )
        if not accepting:
            raise InvalidTransition("the deal does not accept payments now", status=deal.status)
        await self._ledger.post(
            idempotency_key=f"assign:{transfer.id}",
            kind="assign_unidentified",
            deal_id=deal.id,
            ref_type="incoming_transfer",
            ref_id=transfer.id,
            postings=postings.assign_unidentified(
                asset=asset_code(deal.asset, deal.network), deal_id=deal.id, amount=transfer.amount
            ),
        )
        transfer.match_status = MatchStatus.MATCHED
        transfer.match_method = MatchMethod.ADMIN
        transfer.deal_id = deal.id
        self._audit(admin.id, "assign_payment", "incoming_transfer", transfer.id, deal_id=deal.id)
        deals = self.deals
        tx_ref = f"{transfer.network.value}:{transfer.tx_hash}"
        if deal.status is DealStatus.AWAITING_PAYMENT:
            expected = await self._s.scalar(
                sa.select(PaymentRequest.expected_amount).where(
                    PaymentRequest.deal_id == deal.id, PaymentRequest.status == PaymentRequestStatus.ACTIVE
                )
            )
            if expected is not None and await deals.escrow_balance(deal) >= expected:
                return await deals.fund(deal.id, tx_hash=tx_ref)
            return deal  # partial: the buyer tops up or the seller accepts the underpayment
        return await deals.on_late_payment(deal.id, tx_hash=tx_ref)

    async def return_transfer(
        self,
        admin_id: int,
        transfer_id: int,
        *,
        recipient_user_id: int | None = None,
        recipient_address: str | None = None,
    ) -> Payout:
        admin = await self.require(admin_id)
        transfer = await self._lock_unidentified(transfer_id)
        if transfer.provider is Provider.CRYPTOBOT:
            if recipient_user_id is None:
                raise ValidationFailed(
                    "a Telegram user is required for a CryptoBot return", field="recipient"
                )
            address = None
        else:
            if not recipient_address:
                raise ValidationFailed("an address is required", field="recipient")
            address = validate_address(
                transfer.network, recipient_address, allow_testnet=self._settings.testnet_mode
            )
            configured = self._chains.networks.get(transfer.network)
            if (
                configured
                and configured.deposit_address
                and configured.deposit_address.lower() == address.lower()
            ):
                raise ValidationFailed("this is the bot's own address", field="address")
            recipient_user_id = None
        asset = asset_code(Asset.USDT, transfer.network)
        await self._ledger.post(
            idempotency_key=f"return:{transfer.id}",
            kind="reserve_unidentified_return",
            ref_type="incoming_transfer",
            ref_id=transfer.id,
            postings=postings.reserve_unidentified_return(asset=asset, amount=transfer.amount),
        )
        payout = Payout(
            idempotency_key=f"unidentified_return:{transfer.id}",
            incoming_transfer_id=transfer.id,
            kind=PayoutKind.UNIDENTIFIED_RETURN,
            provider=transfer.provider,
            network=transfer.network,
            asset=Asset.USDT,
            recipient_user_id=recipient_user_id,
            recipient_address=address,
            amount=transfer.amount,
            status=PayoutStatus.QUEUED,
            next_attempt_at=self._clock(),
        )
        self._s.add(payout)
        transfer.match_status = MatchStatus.RESOLVED
        await self._s.flush()
        self._audit(admin.id, "return_payment", "incoming_transfer", transfer.id, payout_id=payout.id)
        return payout

    # ── overview ───────────────────────────────────────────────────────────
    async def balances(self) -> list[BalanceRow]:
        rows = await self._s.execute(
            sa.select(
                LedgerAccount.asset,
                LedgerAccount.code,
                LedgerAccount.kind,
                LedgerAccount.wallet,
                sa.func.sum(LedgerAccount.balance),
            )
            .group_by(LedgerAccount.asset, LedgerAccount.code, LedgerAccount.kind, LedgerAccount.wallet)
            .order_by(LedgerAccount.asset)
        )
        result: dict[str, BalanceRow] = {}
        for asset, code, kind, wallet, total in rows.all():
            row = result.setdefault(asset, BalanceRow(asset))
            value = int(total) if kind.value in _DEBIT_NORMAL else -int(total)
            if code == AccountCode.CUSTODY.value:
                row.custody[wallet or "?"] = value
            row.by_code[code] = row.by_code.get(code, 0) + value
        return list(result.values())

    async def search(self, query: str) -> SearchResult:
        q = query.strip()
        result = SearchResult()
        if not q:
            return result
        if _CODE_RE.fullmatch(q.upper().lstrip("#")):
            deal = (
                await self._s.execute(sa.select(Deal).where(Deal.code == q.upper().lstrip("#")))
            ).scalar_one_or_none()
            if deal is not None:
                result.deals.append(deal)
        name = q.lstrip("@")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
            result.users += list(
                (
                    await self._s.execute(sa.select(User).where(sa.func.lower(User.username) == name.lower()))
                ).scalars()
            )
        if q.isdigit():
            user = await self._s.get(User, int(q))
            if user is not None:
                result.users.append(user)
        result.transfers += list(
            (
                await self._s.execute(
                    sa.select(IncomingTransfer).where(IncomingTransfer.tx_hash == q).limit(5)
                )
            ).scalars()
        )
        for user in result.users:
            result.deals += list(
                (
                    await self._s.execute(
                        sa.select(Deal)
                        .where(sa.or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
                        .order_by(Deal.id.desc())
                        .limit(5)
                    )
                ).scalars()
            )
        return result

    async def set_ban(self, admin_id: int, user_id: int, *, banned: bool, reason: str = "") -> User:
        admin = await self.require(admin_id)
        user = await self._s.get(User, user_id, with_for_update=True)
        if user is None:
            raise NotFound("user not found")
        if user.role in ARBITERS and user.id != admin.id and banned:
            raise Forbidden("admins are managed with the CLI")
        user.is_banned = banned
        user.ban_reason = (reason.strip()[:300] or None) if banned else None
        self._audit(admin.id, "ban" if banned else "unban", "user", user.id, reason=reason[:300])
        await self._s.flush()
        return user
