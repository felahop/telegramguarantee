"""All ORM models; importing this package registers every table on ``Base.metadata``."""

from app.db.models.deals import Deal, DealEvent, DealMessage, DealTemplate, Dispute
from app.db.models.ledger import LedgerAccount, LedgerEntry, LedgerTransaction
from app.db.models.ops import AdminAudit, NotificationOutbox, ReconciliationRun
from app.db.models.payments import ChainCursor, DepositAddress, IncomingTransfer, PaymentRequest, TxClaim
from app.db.models.payouts import HotWallet, Payout, PayoutApproval, PayoutAttempt
from app.db.models.users import Blacklist, SavedAddress, User, UsernameHistory

__all__ = [
    "AdminAudit",
    "Blacklist",
    "ChainCursor",
    "Deal",
    "DealEvent",
    "DealMessage",
    "DealTemplate",
    "DepositAddress",
    "Dispute",
    "HotWallet",
    "IncomingTransfer",
    "LedgerAccount",
    "LedgerEntry",
    "LedgerTransaction",
    "NotificationOutbox",
    "PaymentRequest",
    "Payout",
    "PayoutApproval",
    "PayoutAttempt",
    "ReconciliationRun",
    "SavedAddress",
    "TxClaim",
    "User",
    "UsernameHistory",
]
