"""Double-entry ledger.

Conventions:
* entries are signed integers in minor units: **debit = positive, credit = negative**;
* every transaction balances to zero **per asset** (USDT@tron and USDT@bsc never net out);
* asset/expense accounts carry a non-negative balance, liability/revenue a non-positive one —
  enforced by a CHECK on ``ledger_accounts.balance`` (the cache is maintained by a DB trigger),
  so overdrawing an escrow, a payable or a custody wallet fails inside the database;
* the ledger is append-only: mistakes are corrected with :meth:`Ledger.reverse`.

Posting builders at the bottom of the module describe the canonical money movements of a deal
(see docs/ARCHITECTURE.md §11). Services compose them with state transitions in one DB transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LedgerAccount, LedgerEntry, LedgerTransaction
from app.domain.enums import AccountKind
from app.domain.errors import IdempotencyConflict, LedgerError, NotFound


class AccountCode(StrEnum):
    CUSTODY = "custody"  # our hot wallet / deposit address / CryptoBot app balance
    ESCROW = "escrow"  # per deal: buyer's money held by the bot
    PAYABLE = "payable"  # per deal: owed to seller/buyer, payout not yet confirmed
    UNIDENTIFIED = "unidentified"  # deposits we could not match to a deal
    PAYABLE_UNIDENTIFIED = "payable_unidentified"  # unidentified money being returned
    FEE_REVENUE = "fee_revenue"
    NETWORK_FEE_RECOVERED = "network_fee_recovered"  # fixed network fee charged to users (in USDT)
    NETWORK_FEE_EXPENSE = "network_fee_expense"  # gas actually burnt (in the native coin)
    PROVIDER_FEE_EXPENSE = "provider_fee_expense"  # fee withheld by a payment provider (CryptoBot)
    ADJUSTMENTS = "adjustments"  # manual corrections; admin confirmation + audit


ACCOUNT_KINDS: dict[AccountCode, AccountKind] = {
    AccountCode.CUSTODY: AccountKind.ASSET,
    AccountCode.ESCROW: AccountKind.LIABILITY,
    AccountCode.PAYABLE: AccountKind.LIABILITY,
    AccountCode.UNIDENTIFIED: AccountKind.LIABILITY,
    AccountCode.PAYABLE_UNIDENTIFIED: AccountKind.LIABILITY,
    AccountCode.FEE_REVENUE: AccountKind.REVENUE,
    AccountCode.NETWORK_FEE_RECOVERED: AccountKind.REVENUE,
    AccountCode.NETWORK_FEE_EXPENSE: AccountKind.EXPENSE,
    AccountCode.PROVIDER_FEE_EXPENSE: AccountKind.EXPENSE,
    AccountCode.ADJUSTMENTS: AccountKind.EQUITY,
}
_ALLOW_NEGATIVE = frozenset({AccountCode.ADJUSTMENTS})
_DEBIT_NORMAL = frozenset({AccountKind.ASSET, AccountKind.EXPENSE})


@dataclass(frozen=True, order=True)
class AccountRef:
    code: AccountCode
    asset: str
    wallet: str | None = None
    deal_id: int | None = None
    user_id: int | None = None

    @property
    def kind(self) -> AccountKind:
        return ACCOUNT_KINDS[self.code]

    def identity(self) -> tuple[str, str, str | None, int | None, int | None]:
        return (self.code.value, self.asset, self.wallet, self.deal_id, self.user_id)

    # Constructors for the accounts used by the domain.
    @classmethod
    def custody(cls, asset: str, wallet: str) -> AccountRef:
        return cls(AccountCode.CUSTODY, asset, wallet=wallet)

    @classmethod
    def escrow(cls, asset: str, deal_id: int) -> AccountRef:
        return cls(AccountCode.ESCROW, asset, deal_id=deal_id)

    @classmethod
    def payable(cls, asset: str, deal_id: int) -> AccountRef:
        return cls(AccountCode.PAYABLE, asset, deal_id=deal_id)

    @classmethod
    def unidentified(cls, asset: str) -> AccountRef:
        return cls(AccountCode.UNIDENTIFIED, asset)

    @classmethod
    def payable_unidentified(cls, asset: str) -> AccountRef:
        return cls(AccountCode.PAYABLE_UNIDENTIFIED, asset)

    @classmethod
    def fee_revenue(cls, asset: str) -> AccountRef:
        return cls(AccountCode.FEE_REVENUE, asset)

    @classmethod
    def network_fee_recovered(cls, asset: str) -> AccountRef:
        return cls(AccountCode.NETWORK_FEE_RECOVERED, asset)

    @classmethod
    def network_fee_expense(cls, native_asset: str) -> AccountRef:
        return cls(AccountCode.NETWORK_FEE_EXPENSE, native_asset)

    @classmethod
    def provider_fee_expense(cls, asset: str) -> AccountRef:
        return cls(AccountCode.PROVIDER_FEE_EXPENSE, asset)

    @classmethod
    def adjustments(cls, asset: str) -> AccountRef:
        return cls(AccountCode.ADJUSTMENTS, asset)


@dataclass(frozen=True)
class Posting:
    account: AccountRef
    amount: int  # debit > 0, credit < 0

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise TypeError("posting amount must be int minor units")
        if self.amount == 0:
            raise LedgerError("zero posting", account=self.account.identity())


def debit(account: AccountRef, amount: int) -> Posting:
    _require_positive(amount)
    return Posting(account, amount)


def credit(account: AccountRef, amount: int) -> Posting:
    _require_positive(amount)
    return Posting(account, -amount)


def _require_positive(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise LedgerError("debit/credit amounts must be positive int", amount=repr(amount))


@dataclass
class _AccountCache:
    ids: dict[tuple[str, str, str | None, int | None, int | None], int] = field(default_factory=dict)


class Ledger:
    """Ledger operations within the caller's DB transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._cache = _AccountCache()

    # ── writing ────────────────────────────────────────────────────────────
    async def post(
        self,
        *,
        idempotency_key: str,
        kind: str,
        postings: Sequence[Posting],
        deal_id: int | None = None,
        ref_type: str | None = None,
        ref_id: int | None = None,
        meta: dict[str, Any] | None = None,
        _reverses_id: int | None = None,
    ) -> LedgerTransaction:
        """Record a balanced transaction. Replaying the same key with the same postings is a no-op."""
        _validate_balanced(postings)
        fingerprint = _fingerprint(kind, postings)

        inserted = await self._session.execute(
            pg_insert(LedgerTransaction)
            .values(
                idempotency_key=idempotency_key,
                kind=kind,
                deal_id=deal_id,
                ref_type=ref_type,
                ref_id=ref_id,
                fingerprint=fingerprint,
                reverses_id=_reverses_id,
                meta=meta or {},
            )
            .on_conflict_do_nothing(index_elements=[LedgerTransaction.idempotency_key])
            .returning(LedgerTransaction.id)
        )
        txn_id = inserted.scalar_one_or_none()
        if txn_id is None:
            existing = await self._get_transaction(idempotency_key)
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict(
                    "ledger idempotency key reused with different postings", idempotency_key=idempotency_key
                )
            return existing

        account_ids = await self._resolve_accounts(p.account for p in postings)
        # Lock account rows (balance trigger) in a stable order: no deadlocks between postings.
        ordered = sorted(postings, key=lambda p: account_ids[p.account.identity()])
        rows = [
            {
                "transaction_id": txn_id,
                "account_id": account_ids[p.account.identity()],
                "asset": p.account.asset,
                "amount": p.amount,
            }
            for p in ordered
        ]
        try:
            async with self._session.begin_nested():
                await self._session.execute(sa.insert(LedgerEntry), rows)
        except IntegrityError as exc:
            if "balance_sign" in str(exc.orig):
                raise LedgerError(
                    "insufficient balance: posting would overdraw an account",
                    idempotency_key=idempotency_key,
                    kind=kind,
                ) from exc
            raise
        return await self._get_transaction(idempotency_key)

    async def reverse(self, *, original_key: str, idempotency_key: str, reason: str) -> LedgerTransaction:
        original = await self._get_transaction(original_key)
        entries = (
            await self._session.execute(
                sa.select(LedgerEntry, LedgerAccount)
                .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
                .where(LedgerEntry.transaction_id == original.id)
            )
        ).all()
        postings = [
            Posting(
                AccountRef(
                    AccountCode(account.code),
                    account.asset,
                    wallet=account.wallet,
                    deal_id=account.deal_id,
                    user_id=account.user_id,
                ),
                -entry.amount,
            )
            for entry, account in entries
        ]
        try:
            async with self._session.begin_nested():
                return await self.post(
                    idempotency_key=idempotency_key,
                    kind=f"reversal:{original.kind}",
                    postings=postings,
                    deal_id=original.deal_id,
                    ref_type="ledger_transaction",
                    ref_id=original.id,
                    meta={"reason": reason, "original_key": original_key},
                    _reverses_id=original.id,
                )
        except IntegrityError as exc:
            if "reverses_id" in str(exc.orig):
                raise LedgerError("transaction already reversed", original_key=original_key) from exc
            raise

    # ── reading ────────────────────────────────────────────────────────────
    async def balance(self, account: AccountRef) -> int:
        """Raw signed balance (debit positive)."""
        row = await self._session.execute(sa.select(LedgerAccount.balance).where(*_identity_filter(account)))
        return row.scalar_one_or_none() or 0

    async def normal_balance(self, account: AccountRef) -> int:
        """Balance on the account's normal side: what the escrow holds, what custody has, etc."""
        raw = await self.balance(account)
        return raw if account.kind in _DEBIT_NORMAL else -raw

    async def find_inconsistent_accounts(self) -> list[tuple[int, int, int]]:
        """Accounts whose cached balance differs from the sum of entries: (id, cached, actual)."""
        actual = (
            sa.select(LedgerEntry.account_id, sa.func.sum(LedgerEntry.amount).label("total"))
            .group_by(LedgerEntry.account_id)
            .subquery()
        )
        rows = await self._session.execute(
            sa.select(LedgerAccount.id, LedgerAccount.balance, sa.func.coalesce(actual.c.total, 0))
            .outerjoin(actual, actual.c.account_id == LedgerAccount.id)
            .where(LedgerAccount.balance != sa.func.coalesce(actual.c.total, 0))
        )
        return [(int(a), int(b), int(c)) for a, b, c in rows.all()]

    # ── internals ──────────────────────────────────────────────────────────
    async def _get_transaction(self, idempotency_key: str) -> LedgerTransaction:
        txn = (
            await self._session.execute(
                sa.select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if txn is None:
            raise NotFound("ledger transaction not found", idempotency_key=idempotency_key)
        return txn

    async def _resolve_accounts(
        self, accounts: Iterable[AccountRef]
    ) -> dict[tuple[str, str, str | None, int | None, int | None], int]:
        result: dict[tuple[str, str, str | None, int | None, int | None], int] = {}
        for account in sorted(set(accounts)):
            key = account.identity()
            if key not in self._cache.ids:
                self._cache.ids[key] = await self._get_or_create_account(account)
            result[key] = self._cache.ids[key]
        return result

    async def _get_or_create_account(self, account: AccountRef) -> int:
        inserted = await self._session.execute(
            pg_insert(LedgerAccount)
            .values(
                code=account.code.value,
                kind=account.kind,
                asset=account.asset,
                wallet=account.wallet,
                deal_id=account.deal_id,
                user_id=account.user_id,
                allow_negative=account.code in _ALLOW_NEGATIVE,
            )
            .on_conflict_do_nothing(constraint="uq_ledger_accounts_identity")
            .returning(LedgerAccount.id)
        )
        account_id = inserted.scalar_one_or_none()
        if account_id is not None:
            return account_id
        existing = await self._session.execute(sa.select(LedgerAccount.id).where(*_identity_filter(account)))
        return existing.scalar_one()


def _identity_filter(account: AccountRef) -> list[sa.ColumnElement[bool]]:
    return [
        LedgerAccount.code == account.code.value,
        LedgerAccount.asset == account.asset,
        LedgerAccount.wallet.is_not_distinct_from(account.wallet),
        LedgerAccount.deal_id.is_not_distinct_from(account.deal_id),
        LedgerAccount.user_id.is_not_distinct_from(account.user_id),
    ]


def _validate_balanced(postings: Sequence[Posting]) -> None:
    if len(postings) < 2:
        raise LedgerError("a transaction needs at least two postings")
    totals: dict[str, int] = defaultdict(int)
    for posting in postings:
        totals[posting.account.asset] += posting.amount
    unbalanced = {asset: total for asset, total in totals.items() if total != 0}
    if unbalanced:
        raise LedgerError("unbalanced transaction", totals=unbalanced)


def _fingerprint(kind: str, postings: Sequence[Posting]) -> str:
    canonical = sorted([*p.account.identity(), p.amount] for p in postings)
    payload = json.dumps([kind, canonical], separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── Canonical postings (docs/ARCHITECTURE.md §11) ────────────────────────────


def deposit_matched(*, asset: str, wallet: str, deal_id: int, amount: int) -> list[Posting]:
    """A final, identified deposit: our custody grows, we now hold it in escrow for the deal."""
    return [
        debit(AccountRef.custody(asset, wallet), amount),
        credit(AccountRef.escrow(asset, deal_id), amount),
    ]


def deposit_unidentified(*, asset: str, wallet: str, amount: int) -> list[Posting]:
    return [debit(AccountRef.custody(asset, wallet), amount), credit(AccountRef.unidentified(asset), amount)]


def provider_deposit(
    *, asset: str, wallet: str, amount: int, provider_fee: int, deal_id: int | None
) -> list[Posting]:
    """A deposit through a provider that withholds a fee: the buyer is credited the full amount
    (escrow, or unidentified when ``deal_id`` is None), custody grows by what actually arrived, and
    the difference is the service's expense."""
    if not 0 <= provider_fee < amount:
        raise LedgerError("provider fee out of range", amount=amount, fee=provider_fee)
    target = AccountRef.escrow(asset, deal_id) if deal_id is not None else AccountRef.unidentified(asset)
    postings = [debit(AccountRef.custody(asset, wallet), amount - provider_fee), credit(target, amount)]
    if provider_fee:
        postings.append(debit(AccountRef.provider_fee_expense(asset), provider_fee))
    return postings


def assign_unidentified(*, asset: str, deal_id: int, amount: int) -> list[Posting]:
    return [debit(AccountRef.unidentified(asset), amount), credit(AccountRef.escrow(asset, deal_id), amount)]


def release_escrow(
    *,
    asset: str,
    deal_id: int,
    to_payable: int,
    fee_revenue: int = 0,
    network_fee_recovered: int = 0,
) -> list[Posting]:
    """Move escrowed money to "owed" (payable) and recognise the service fee and network fee.

    Used on buyer confirmation, on refunds and on dispute resolution. Zero parts are skipped.
    """
    total = to_payable + fee_revenue + network_fee_recovered
    if min(to_payable, fee_revenue, network_fee_recovered) < 0 or total <= 0:
        raise LedgerError("invalid escrow release split", to_payable=to_payable, fee=fee_revenue)
    postings = [debit(AccountRef.escrow(asset, deal_id), total)]
    if to_payable:
        postings.append(credit(AccountRef.payable(asset, deal_id), to_payable))
    if fee_revenue:
        postings.append(credit(AccountRef.fee_revenue(asset), fee_revenue))
    if network_fee_recovered:
        postings.append(credit(AccountRef.network_fee_recovered(asset), network_fee_recovered))
    return postings


def payout_settled(*, asset: str, wallet: str, deal_id: int, amount: int) -> list[Posting]:
    """A payout/refund is final on-chain (or the CryptoBot transfer completed)."""
    return [
        debit(AccountRef.payable(asset, deal_id), amount),
        credit(AccountRef.custody(asset, wallet), amount),
    ]


def reserve_unidentified_return(*, asset: str, amount: int) -> list[Posting]:
    return [
        debit(AccountRef.unidentified(asset), amount),
        credit(AccountRef.payable_unidentified(asset), amount),
    ]


def unidentified_return_settled(*, asset: str, wallet: str, amount: int) -> list[Posting]:
    return [
        debit(AccountRef.payable_unidentified(asset), amount),
        credit(AccountRef.custody(asset, wallet), amount),
    ]


def network_fee_spent(*, native_asset: str, wallet: str, amount: int) -> list[Posting]:
    """Gas burnt by a payout, in the network's native coin (BNB/TRX/ETH/TON/SOL)."""
    return [
        debit(AccountRef.network_fee_expense(native_asset), amount),
        credit(AccountRef.custody(native_asset, wallet), amount),
    ]


def operator_funding(*, asset: str, wallet: str, amount: int) -> list[Posting]:
    """The operator tops up a wallet (gas coins, liquidity): recorded against equity."""
    return [debit(AccountRef.custody(asset, wallet), amount), credit(AccountRef.adjustments(asset), amount)]


def operator_withdrawal(*, asset: str, wallet: str, amount: int) -> list[Posting]:
    """The operator takes money out of a wallet (earned fees) or writes off a confirmed loss."""
    return [debit(AccountRef.adjustments(asset), amount), credit(AccountRef.custody(asset, wallet), amount)]
