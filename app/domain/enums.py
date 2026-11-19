"""Domain enumerations shared by models, services and the bot layer.

Values are persisted as VARCHAR + CHECK constraints (not native PG enums): adding a value is a
plain migration that rewrites the CHECK, without ``ALTER TYPE`` restrictions inside transactions.
"""

from __future__ import annotations

from enum import StrEnum


class Network(StrEnum):
    CRYPTOBOT = "cryptobot"
    BSC = "bsc"
    ETH = "eth"
    TRON = "tron"
    TON = "ton"
    SOL = "sol"

    @property
    def provider(self) -> Provider:
        return Provider.CRYPTOBOT if self is Network.CRYPTOBOT else Provider.ONCHAIN


class Provider(StrEnum):
    CRYPTOBOT = "cryptobot"
    ONCHAIN = "onchain"


class Asset(StrEnum):
    # Product decision: deals are settled in USDT only.
    USDT = "USDT"


class DealStatus(StrEnum):
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    FUNDED = "FUNDED"
    SELLER_CONFIRMED = "SELLER_CONFIRMED"
    BUYER_CONFIRMED = "BUYER_CONFIRMED"
    PAYOUT_PENDING = "PAYOUT_PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DISPUTED = "DISPUTED"
    RESOLVED_BUYER = "RESOLVED_BUYER"
    RESOLVED_SELLER = "RESOLVED_SELLER"
    SPLIT = "SPLIT"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


# Statuses in which the bot holds (or may hold) the buyer's money.
FUNDED_STATUSES: frozenset[DealStatus] = frozenset(
    {DealStatus.FUNDED, DealStatus.SELLER_CONFIRMED, DealStatus.DISPUTED}
)
# Statuses from which nothing else can happen except a late-payment refund.
TERMINAL_STATUSES: frozenset[DealStatus] = frozenset(
    {DealStatus.COMPLETED, DealStatus.REFUNDED, DealStatus.CANCELLED, DealStatus.EXPIRED}
)
# Transit statuses: recorded in the audit log, never persisted as the resting state of a deal.
TRANSIT_STATUSES: frozenset[DealStatus] = frozenset(
    {DealStatus.BUYER_CONFIRMED, DealStatus.RESOLVED_BUYER, DealStatus.RESOLVED_SELLER, DealStatus.SPLIT}
)
EDITABLE_STATUSES: frozenset[DealStatus] = frozenset({DealStatus.CREATED, DealStatus.ACCEPTED})


class PartyRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"

    @property
    def other(self) -> PartyRole:
        return PartyRole.SELLER if self is PartyRole.BUYER else PartyRole.BUYER


class FeePayer(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    SPLIT = "split"


class NetworkFeePayer(StrEnum):
    SELLER = "seller"
    SERVICE = "service"


class UserRole(StrEnum):
    USER = "user"
    ARBITRATOR = "arbitrator"
    ADMIN = "admin"


class DealSource(StrEnum):
    WIZARD = "wizard"
    COMMAND = "command"
    INLINE = "inline"
    REPEAT = "repeat"
    TEMPLATE = "template"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    ARBITRATOR = "arbitrator"
    ADMIN = "admin"
    PROVIDER = "provider"


class DealResolution(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    SPLIT = "split"
    MUTUAL_CANCEL = "mutual_cancel"
    REFUND_AFTER_DEADLINE = "refund_after_deadline"
    LATE_PAYMENT = "late_payment"
    # The payment window closed (or the deal was cancelled) with only part of the money received.
    PARTIAL_PAYMENT_RETURNED = "partial_payment_returned"


class PaymentRequestStatus(StrEnum):
    ACTIVE = "active"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TransferStatus(StrEnum):
    SEEN = "seen"
    FINAL = "final"
    DROPPED = "dropped"
    NEEDS_REVIEW = "needs_review"


class MatchStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    LATE = "late"
    OVERPAID = "overpaid"
    UNIDENTIFIED = "unidentified"
    CLAIMED = "claimed"
    RESOLVED = "resolved"


class MatchMethod(StrEnum):
    INVOICE = "invoice"
    ADDRESS = "address"
    MEMO = "memo"
    REFERENCE = "reference"
    AMOUNT = "amount"
    TX_CLAIM = "tx_claim"
    ADMIN = "admin"


class TxClaimStatus(StrEnum):
    AUTO_MATCHED = "auto_matched"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DepositAddressStatus(StrEnum):
    ASSIGNED = "assigned"
    SWEPT = "swept"
    RETIRED = "retired"


class PayoutKind(StrEnum):
    SELLER_PAYOUT = "seller_payout"
    BUYER_REFUND = "buyer_refund"
    OVERPAY_REFUND = "overpay_refund"
    SPLIT_BUYER = "split_buyer"
    SPLIT_SELLER = "split_seller"
    UNIDENTIFIED_RETURN = "unidentified_return"


SELLER_BOUND_PAYOUTS: frozenset[PayoutKind] = frozenset({PayoutKind.SELLER_PAYOUT, PayoutKind.SPLIT_SELLER})
BUYER_BOUND_PAYOUTS: frozenset[PayoutKind] = frozenset(
    {PayoutKind.BUYER_REFUND, PayoutKind.OVERPAY_REFUND, PayoutKind.SPLIT_BUYER}
)


class PayoutStatus(StrEnum):
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    SIGNING = "signing"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


OPEN_PAYOUT_STATUSES: frozenset[PayoutStatus] = frozenset(
    set(PayoutStatus) - {PayoutStatus.CONFIRMED, PayoutStatus.CANCELLED}
)


class PayoutAttemptStatus(StrEnum):
    SIGNED = "signed"
    BROADCAST = "broadcast"
    FINAL_OK = "final_ok"
    REVERTED = "reverted"
    EXPIRED = "expired"
    REPLACED = "replaced"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class DisputeStatus(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


OPEN_DISPUTE_STATUSES: frozenset[DisputeStatus] = frozenset({DisputeStatus.OPEN, DisputeStatus.IN_REVIEW})


class DisputeReason(StrEnum):
    USER = "user"
    BUYER_TIMEOUT = "buyer_timeout"
    REFUND_OBJECTION = "refund_objection"


class DisputeDecision(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    SPLIT = "split"


class AccountKind(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    REVENUE = "revenue"
    EXPENSE = "expense"
    EQUITY = "equity"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class TimerKind(StrEnum):
    OFFER_EXPIRE = "offer_expire"
    PAY_START_EXPIRE = "pay_start_expire"
    PAYMENT_EXPIRE = "payment_expire"
    EXECUTION_DEADLINE = "execution_deadline"
    REFUND_WINDOW_ELAPSED = "refund_window_elapsed"
    BUYER_REMINDER = "buyer_reminder"
    AUTO_DISPUTE = "auto_dispute"
    DISPUTE_SLA = "dispute_sla"
