"""Background control: is the money where the ledger says, is the ledger consistent, is anything stuck.

A run never moves money. It records a ``ReconciliationRun`` per wallet, alerts admins through the
outbox, and does two bits of housekeeping: releasing unique-amount tags after their cooldown and
reminding buyers who still have not given a refund address.

Balance check
    What the provider/chain reports for a wallet is compared with the ledger custody account of
    that wallet. The two are read at different moments, so a payment or a transfer landing in
    between shows up as a one-off difference: an alert needs the difference to have the same sign
    in a run at least ``reconcile_confirm_after`` older. Missing money up to the amount of payouts
    whose outcome is unknown counts as in flight (the stuck-payout checks watch those payouts). Any
    other shortage is critical and, with ``reconcile_auto_pause``, stops payouts from that wallet
    until an admin resumes them: a bug paying twice or a leaked key must not keep draining it.

Alerts are deduplicated on the identity of the incident (plus the day for long-lasting ones), so a
persisting problem is reported once a day, not every 15 minutes, while a new one is reported at once.

Clocks: columns written by the application (timers, outbox, tag release, ``first_seen_at`` of
incoming transfers) are compared with the application clock; columns defaulted by the database
(``payouts.created_at``) with the database clock, so a skew between the two never raises false alarms.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chains.base import ChainAdapter
from app.config import ChainsConfig, Settings
from app.db.models import (
    ChainCursor,
    Deal,
    HotWallet,
    IncomingTransfer,
    LedgerAccount,
    NotificationOutbox,
    PaymentRequest,
    Payout,
    PayoutPause,
    ReconciliationRun,
)
from app.domain.enums import (
    OPEN_PAYOUT_STATUSES,
    TERMINAL_STATUSES,
    Asset,
    MatchStatus,
    Network,
    OutboxStatus,
    PaymentRequestStatus,
    PayoutKind,
    PayoutStatus,
    Provider,
)
from app.domain.money import NATIVE_DECIMALS, asset_code, format_amount
from app.logging import get_logger
from app.providers.cryptobot.client import CryptoPayClient
from app.services import notifications
from app.services.ledger import AccountCode, AccountRef, Ledger
from app.services.notifications import Notice
from app.services.payments import CRYPTOBOT_WALLET, PAYOUT_OUTCOME_UNKNOWN

log = get_logger(__name__)

# Only one reconciliation at a time across all workers and the CLI (pg_try_advisory_xact_lock).
_LOCK_ID = 0x5EC0_0001
_LIABILITIES = (
    AccountCode.ESCROW,
    AccountCode.PAYABLE,
    AccountCode.UNIDENTIFIED,
    AccountCode.PAYABLE_UNIDENTIFIED,
)
_LIST_LIMIT = 50  # per check: an incident with thousands of items is still one screen of alerts


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str  # for admins, Russian, no secrets
    key: str  # identity of the incident: alerts are deduplicated on it
    deal_id: int | None = None
    payout_id: int | None = None


@dataclass(frozen=True)
class ObservedBalance:
    """What really sits in a wallet, per the provider or the chain."""

    asset: str  # ledger asset code, e.g. "USDT@cryptobot"
    wallet: str
    amount: int  # minor units


class BalanceSource(Protocol):
    name: str

    async def balances(self) -> list[ObservedBalance]: ...


class CryptoBotBalances:
    """The CryptoBot app balance (available + on hold) of USDT."""

    name = "cryptobot"

    def __init__(self, client: CryptoPayClient, decimals: int) -> None:
        self._client = client
        self._decimals = decimals

    async def balances(self) -> list[ObservedBalance]:
        total = Decimal(0)
        for balance in await self._client.get_balance():
            if balance.currency_code.upper() == Asset.USDT.value:
                total += balance.available + balance.onhold
        # Floor: if the API ever reports more digits than the asset has, we never overstate.
        amount = int(total.scaleb(self._decimals).to_integral_value(rounding=ROUND_DOWN))
        return [ObservedBalance(asset_code(Asset.USDT, Network.CRYPTOBOT), CRYPTOBOT_WALLET, amount)]


class OnchainBalances:
    """USDT on our deposit addresses at the final head, read through the network's adapter."""

    def __init__(self, adapter: ChainAdapter) -> None:
        self._adapter = adapter
        self.name = adapter.network.value

    async def balances(self) -> list[ObservedBalance]:
        asset = asset_code(Asset.USDT, self._adapter.network)
        return [
            ObservedBalance(asset, address, await self._adapter.token_balance(address))
            for address in self._adapter.addresses
        ]


def balance_sources(
    chains: ChainsConfig,
    cryptopay: CryptoPayClient | None,
    adapters: Iterable[ChainAdapter] = (),
) -> list[BalanceSource]:
    """Every wallet we can read a real balance of: the CryptoBot app and our deposit addresses."""
    sources: list[BalanceSource] = []
    if cryptopay is not None:
        sources.append(
            CryptoBotBalances(cryptopay, chains.spec(Network.CRYPTOBOT, include_disabled=True).decimals)
        )
    sources += [OnchainBalances(adapter) for adapter in adapters]
    return sources


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    runs: list[ReconciliationRun] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    tags_released: int = 0
    reminders_sent: int = 0
    source_errors: list[str] = field(default_factory=list)
    skipped: bool = False  # another reconciliation was running

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.CRITICAL]


class Reconciler:
    def __init__(
        self, session: AsyncSession, settings: Settings, chains: ChainsConfig, *, now: datetime
    ) -> None:
        self._s = session
        self._settings = settings
        self._chains = chains
        self._now = now  # application clock
        self._db_now: datetime | None = None
        self._day = now.strftime("%Y%m%d")

    async def run(self, observed: Sequence[ObservedBalance]) -> Report:
        self._db_now = (await self._s.execute(sa.select(sa.func.now()))).scalar_one()
        report = Report()
        for seen in observed:
            run, finding = await self.check_balance(seen)
            report.runs.append(run)
            if finding is not None:
                report.findings.append(finding)
                if finding.severity is Severity.CRITICAL and await self._auto_pause(seen.wallet, finding):
                    report.paused.append(seen.wallet)
        report.findings += await self.check_ledger()
        report.findings += await self.check_stuck()
        report.reminders_sent = await self.remind_refund_addresses()
        report.tags_released = await self.release_tags()
        await self.alert(report.findings)
        for finding in report.findings:
            log.warning(
                "reconciliation_finding",
                check=finding.check,
                severity=finding.severity.value,
                deal_id=finding.deal_id,
                payout_id=finding.payout_id,
                message=finding.message,
            )
        return report

    # ── balances ───────────────────────────────────────────────────────────
    async def check_balance(self, seen: ObservedBalance) -> tuple[ReconciliationRun, Finding | None]:
        ledger = await Ledger(self._s).normal_balance(AccountRef.custody(seen.asset, seen.wallet))
        in_flight = await self._in_flight(seen)
        diff = seen.amount - ledger
        previous = (
            await self._s.execute(
                sa.select(ReconciliationRun)
                .where(
                    ReconciliationRun.wallet == seen.wallet,
                    ReconciliationRun.asset == seen.asset,
                    ReconciliationRun.created_at <= self._now - self._settings.reconcile_confirm_after,
                )
                .order_by(ReconciliationRun.created_at.desc(), ReconciliationRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        run = ReconciliationRun(
            wallet=seen.wallet,
            asset=seen.asset,
            chain_balance=seen.amount,
            ledger_balance=ledger,
            in_flight=in_flight,
            diff=diff,
            ok=diff == 0,
            created_at=self._now,
        )
        self._s.add(run)
        log.info(
            "reconciliation_balance",
            asset=seen.asset,
            wallet=seen.wallet,
            observed=seen.amount,
            ledger=ledger,
            in_flight=in_flight,
            diff=diff,
        )
        confirmed = previous is not None and bool(previous.diff) and (previous.diff > 0) == (diff > 0)  # type: ignore[operator]
        if diff == 0 or not confirmed or (diff < 0 and -diff <= in_flight):
            return run, None
        fmt = self._formatter(seen.asset)
        where = _wallet_label(seen.wallet)
        if diff < 0:
            pending = f" Из них {fmt(in_flight)} — выплаты с неизвестным исходом." if in_flight else ""
            finding = Finding(
                "balance",
                Severity.CRITICAL,
                f"Недостача на {where}: по ledger {fmt(ledger)}, фактически {fmt(seen.amount)}, "
                f"не хватает {fmt(-diff)} {seen.asset}.{pending} Возможны двойная выплата, вывод "
                "в обход бота или утечка ключа/токена.",
                key=f"balance:{seen.asset}:{seen.wallet}:{diff}:{self._day}",
            )
        else:
            finding = Finding(
                "balance",
                Severity.WARNING,
                f"Излишек на {where}: по ledger {fmt(ledger)}, фактически {fmt(seen.amount)}, "
                f"лишние {fmt(diff)} {seen.asset}. Обычно это пополнение, не записанное в ledger "
                "(python -m app.cli record-funding), или платёж, который ещё не обработан.",
                key=f"balance:{seen.asset}:{seen.wallet}:{diff}:{self._day}",
            )
        return run, finding

    async def _in_flight(self, seen: ObservedBalance) -> int:
        """Payouts from this wallet that may already have left it but are not booked yet."""
        coin, _, network = seen.asset.partition("@")
        if coin != Asset.USDT.value:
            return 0
        query = sa.select(sa.func.coalesce(sa.func.sum(Payout.amount), 0)).where(
            Payout.network == Network(network),
            sa.or_(
                Payout.status.in_([PayoutStatus.SIGNING, PayoutStatus.BROADCAST]),
                sa.and_(
                    Payout.status == PayoutStatus.FAILED_RETRYABLE,
                    Payout.last_error.like(f"{PAYOUT_OUTCOME_UNKNOWN}:%"),
                ),
            ),
        )
        if seen.wallet != CRYPTOBOT_WALLET:
            query = query.join(HotWallet, HotWallet.id == Payout.hot_wallet_id).where(
                HotWallet.address == seen.wallet
            )
        return int((await self._s.execute(query)).scalar_one())

    async def _auto_pause(self, wallet: str, finding: Finding) -> bool:
        if not self._settings.reconcile_auto_pause:
            return False
        return await pause_payouts(self._s, wallet, reason=finding.message[:500], paused_by=None)

    # ── ledger invariants ──────────────────────────────────────────────────
    async def check_ledger(self) -> list[Finding]:
        findings: list[Finding] = []
        for account_id, cached, actual in await Ledger(self._s).find_inconsistent_accounts():
            account = await self._s.get(LedgerAccount, account_id)
            label = f"{account.code} {account.asset}" if account else "?"
            findings.append(
                Finding(
                    "ledger_cache",
                    Severity.CRITICAL,
                    f"Кэш баланса счёта #{account_id} ({label}) = {cached}, а сумма проводок = {actual} "
                    "(минимальные единицы). Ledger изменён в обход триггеров.",
                    key=f"ledger_cache:{account_id}:{cached}:{actual}",
                )
            )
        unbalanced = await self._s.execute(
            sa.select(LedgerAccount.asset, sa.func.sum(LedgerAccount.balance))
            .group_by(LedgerAccount.asset)
            .having(sa.func.sum(LedgerAccount.balance) != 0)
        )
        for asset, total in unbalanced.all():
            findings.append(
                Finding(
                    "ledger_trial_balance",
                    Severity.CRITICAL,
                    f"Сумма всех счетов {asset} = {int(total)} вместо 0: нарушена двойная запись.",
                    key=f"trial_balance:{asset}:{int(total)}",
                )
            )
        findings += await self._closed_deals_with_money()
        findings += await self._payables_match_payouts()
        findings += await self._solvency()
        return findings

    async def _closed_deals_with_money(self) -> list[Finding]:
        rows = await self._s.execute(
            sa.select(
                Deal.id,
                Deal.code,
                Deal.status,
                LedgerAccount.code,
                LedgerAccount.asset,
                LedgerAccount.balance,
            )
            .join(LedgerAccount, LedgerAccount.deal_id == Deal.id)
            .where(
                Deal.status.in_(TERMINAL_STATUSES),
                LedgerAccount.code.in_([AccountCode.ESCROW.value, AccountCode.PAYABLE.value]),
                LedgerAccount.balance != 0,
            )
            .order_by(Deal.id)
            .limit(_LIST_LIMIT)
        )
        return [
            Finding(
                "closed_deal_money",
                Severity.CRITICAL,
                f"Сделка {code} закрыта ({status.value}), но на счёте {account} осталось "
                f"{self._formatter(asset)(-int(balance))} {asset}.",
                key=f"closed_deal_money:{deal_id}:{account}:{int(balance)}",
                deal_id=deal_id,
            )
            for deal_id, code, status, account, asset, balance in rows.all()
        ]

    async def _payables_match_payouts(self) -> list[Finding]:
        """What the ledger owes for a deal must equal its unfinished payouts (no more, no less)."""
        owed: dict[int, int] = {
            int(deal_id or 0): -int(total)
            for deal_id, total in (
                await self._s.execute(
                    sa.select(LedgerAccount.deal_id, sa.func.sum(LedgerAccount.balance))
                    .where(
                        LedgerAccount.code == AccountCode.PAYABLE.value, LedgerAccount.deal_id.is_not(None)
                    )
                    .group_by(LedgerAccount.deal_id)
                    .having(sa.func.sum(LedgerAccount.balance) != 0)
                )
            ).all()
        }
        queued: dict[int, int] = {
            int(deal_id or 0): int(total)
            for deal_id, total in (
                await self._s.execute(
                    sa.select(Payout.deal_id, sa.func.sum(Payout.amount))
                    .where(Payout.status.in_(OPEN_PAYOUT_STATUSES), Payout.deal_id.is_not(None))
                    .group_by(Payout.deal_id)
                )
            ).all()
        }
        findings: list[Finding] = []
        for deal_id in sorted(owed.keys() | queued.keys())[:_LIST_LIMIT]:
            a, b = owed.get(deal_id, 0), queued.get(deal_id, 0)
            if a == b:
                continue
            deal = await self._s.get(Deal, deal_id)
            if deal is None:  # pragma: no cover - payables and payouts reference existing deals
                continue
            fmt = self._formatter(asset_code(deal.asset, deal.network))
            findings.append(
                Finding(
                    "payable_mismatch",
                    Severity.CRITICAL,
                    f"Сделка {deal.code}: к выплате по ledger {fmt(a)}, а в незавершённых выплатах {fmt(b)}.",
                    key=f"payable:{deal_id}:{a}:{b}",
                    deal_id=deal_id,
                )
            )
        returns_owed = {
            str(asset): -int(total)
            for asset, total in (
                await self._s.execute(
                    sa.select(LedgerAccount.asset, sa.func.sum(LedgerAccount.balance))
                    .where(LedgerAccount.code == AccountCode.PAYABLE_UNIDENTIFIED.value)
                    .group_by(LedgerAccount.asset)
                )
            ).all()
        }
        returns_queued: dict[str, int] = {}
        for network, asset, total in (
            await self._s.execute(
                sa.select(Payout.network, Payout.asset, sa.func.sum(Payout.amount))
                .where(Payout.kind == PayoutKind.UNIDENTIFIED_RETURN, Payout.status.in_(OPEN_PAYOUT_STATUSES))
                .group_by(Payout.network, Payout.asset)
            )
        ).all():
            returns_queued[asset_code(asset, network)] = int(total)
        for code in sorted(returns_owed.keys() | returns_queued.keys()):
            a, b = returns_owed.get(code, 0), returns_queued.get(code, 0)
            if a != b:
                fmt = self._formatter(code)
                findings.append(
                    Finding(
                        "payable_mismatch",
                        Severity.CRITICAL,
                        f"Возвраты неопознанных {code}: по ledger {fmt(a)}, "
                        f"в незавершённых выплатах {fmt(b)}.",
                        key=f"payable_unidentified:{code}:{a}:{b}",
                    )
                )
        return findings

    async def _solvency(self) -> list[Finding]:
        """Wallets must hold at least what the service owes to users (escrow, payouts, unidentified)."""
        rows = await self._s.execute(
            sa.select(LedgerAccount.asset, LedgerAccount.code, sa.func.sum(LedgerAccount.balance))
            .where(LedgerAccount.code.in_([AccountCode.CUSTODY.value, *[c.value for c in _LIABILITIES]]))
            .group_by(LedgerAccount.asset, LedgerAccount.code)
        )
        held: dict[str, int] = {}
        owed: dict[str, int] = {}
        for asset, code, total in rows.all():
            if code == AccountCode.CUSTODY.value:
                held[asset] = held.get(asset, 0) + int(total)
            else:
                owed[asset] = owed.get(asset, 0) - int(total)
        findings: list[Finding] = []
        for asset in sorted(owed):
            short = owed[asset] - held.get(asset, 0)
            if short > 0:
                fmt = self._formatter(asset)
                findings.append(
                    Finding(
                        "solvency",
                        Severity.WARNING,
                        f"Обязательства перед пользователями по {asset} ({fmt(owed[asset])}) больше, чем "
                        f"числится на кошельках ({fmt(held.get(asset, 0))}): не хватает {fmt(short)}. "
                        "Обычно это комиссии провайдера по ещё не закрытым сделкам — пополните баланс "
                        "и запишите пополнение (record-funding).",
                        key=f"solvency:{asset}:{short}:{self._day}",
                    )
                )
        return findings

    # ── stuck work ─────────────────────────────────────────────────────────
    async def check_stuck(self) -> list[Finding]:
        findings: list[Finding] = []
        findings += await self._payout_queue_lag()
        findings += await self._payouts_needing_attention()
        findings += await self._old_unidentified()
        findings += await self._timers_lag()
        findings += await self._outbox_lag()
        findings += await self._watchers_lag()
        return findings

    async def _watchers_lag(self) -> list[Finding]:
        """A watcher that stopped advancing means payments are not being credited."""
        db_now = self._db_clock()
        enabled = [n for n in self._chains.enabled_networks() if n.provider is not Provider.CRYPTOBOT]
        if not enabled:
            return []
        rows = await self._s.execute(
            sa.select(ChainCursor).where(
                ChainCursor.network.in_(enabled),
                ChainCursor.updated_at < db_now - self._settings.worker_lag_alert,
            )
        )
        return [
            Finding(
                "chain_watcher",
                Severity.CRITICAL,
                f"Наблюдатель сети {c.network.value} не продвигается {_age(db_now - c.updated_at)}: оплаты в "
                f"этой сети не зачисляются. Последняя ошибка: {(c.last_error or '—')[:200]}",
                key=f"chain_watcher:{c.network.value}:{c.cursor_final}",
            )
            for c in rows.scalars()
        ]

    async def _payout_queue_lag(self) -> list[Finding]:
        paused = set((await self._s.execute(sa.select(PayoutPause.wallet))).scalars())
        query = sa.select(Payout).where(
            Payout.status.in_([PayoutStatus.QUEUED, PayoutStatus.FAILED_RETRYABLE]),
            Payout.next_attempt_at < self._now - self._settings.worker_lag_alert,
        )
        if CRYPTOBOT_WALLET in paused:
            query = query.where(Payout.network != Network.CRYPTOBOT)  # held on purpose
        lagging = list((await self._s.execute(query.order_by(Payout.next_attempt_at))).scalars())
        if not lagging:
            return []
        oldest = lagging[0]
        assert oldest.next_attempt_at is not None  # noqa: S101 - filtered above
        return [
            Finding(
                "payout_queue",
                Severity.CRITICAL,
                f"Очередь выплат стоит: {len(lagging)} шт. ждут дольше срока, старейшая #{oldest.id} — "
                f"{_age(self._now - oldest.next_attempt_at)}. Проверьте воркер и доступ к провайдеру.",
                key=f"payout_queue:{oldest.id}:{oldest.next_attempt_at.isoformat()}",
                payout_id=oldest.id,
            )
        ]

    async def _payouts_needing_attention(self) -> list[Finding]:
        db_now = self._db_clock()
        s = self._settings
        rows = await self._s.execute(
            sa.select(Payout)
            .where(
                sa.or_(
                    Payout.status == PayoutStatus.FAILED_FINAL,
                    sa.and_(
                        Payout.status == PayoutStatus.FAILED_RETRYABLE,
                        Payout.created_at < db_now - s.stuck_payout_after,
                    ),
                    sa.and_(
                        Payout.status == PayoutStatus.AWAITING_APPROVAL,
                        Payout.created_at < db_now - s.payout_approval_sla,
                    ),
                    sa.and_(
                        Payout.status == PayoutStatus.AWAITING_ADDRESS,
                        Payout.created_at < db_now - 3 * s.refund_address_reminder_interval,
                    ),
                )
            )
            .order_by(Payout.id)
            .limit(_LIST_LIMIT)
        )
        findings: list[Finding] = []
        for p in rows.scalars():
            amount = self._formatter(asset_code(p.asset, p.network))(p.amount)
            age = _age(db_now - p.created_at)
            if p.status is PayoutStatus.FAILED_FINAL:
                severity, text = (
                    Severity.CRITICAL,
                    f"Выплата #{p.id} ({amount} USDT) остановлена с ошибкой: {p.last_error or '—'}. "
                    "Разберитесь и нажмите «Повторить» в админке.",
                )
            elif p.status is PayoutStatus.FAILED_RETRYABLE:
                severity, text = (
                    Severity.WARNING,
                    f"Выплата #{p.id} ({amount} USDT) не проходит уже {age}, попыток: {p.attempts}. "
                    f"Последняя ошибка: {p.last_error or '—'}.",
                )
            elif p.status is PayoutStatus.AWAITING_APPROVAL:
                severity, text = (
                    Severity.WARNING,
                    f"Выплата #{p.id} ({amount} USDT) ждёт подтверждения админа уже {age}.",
                )
            else:
                severity, text = (
                    Severity.WARNING,
                    f"Возврат #{p.id} ({amount} USDT): покупатель не указывает адрес уже {age}, "
                    "напоминания отправляются раз в сутки.",
                )
            findings.append(
                Finding(
                    f"payout_{p.status.value}",
                    severity,
                    text,
                    key=f"payout:{p.id}:{p.status.value}:{self._day}",
                    deal_id=p.deal_id,
                    payout_id=p.id,
                )
            )
        return findings

    async def _old_unidentified(self) -> list[Finding]:
        count, oldest = (
            await self._s.execute(
                sa.select(sa.func.count(), sa.func.min(IncomingTransfer.first_seen_at)).where(
                    IncomingTransfer.match_status == MatchStatus.UNIDENTIFIED,
                    IncomingTransfer.first_seen_at < self._now - self._settings.unidentified_sla,
                )
            )
        ).one()
        if not count:
            return []
        return [
            Finding(
                "unidentified",
                Severity.WARNING,
                f"Неопознанные платежи ждут разбора: {count} шт., старейший — {_age(self._now - oldest)}. "
                "Привяжите к сделке или верните отправителю.",
                key=f"unidentified:{count}:{self._day}",
            )
        ]

    async def _timers_lag(self) -> list[Finding]:
        rows = list(
            (
                await self._s.execute(
                    sa.select(Deal.id, Deal.code, Deal.next_action_at)
                    .where(Deal.next_action_at < self._now - self._settings.worker_lag_alert)
                    .order_by(Deal.next_action_at)
                )
            ).all()
        )
        if not rows:
            return []
        deal_id, code, due = rows[0]
        assert due is not None  # noqa: S101 - filtered by the query
        return [
            Finding(
                "timers",
                Severity.CRITICAL,
                f"Таймеры сделок не обрабатываются: просрочено {len(rows)}, старейший — сделка {code}, "
                f"{_age(self._now - due)}. Проверьте воркер.",
                key=f"timers:{deal_id}:{due.isoformat()}",
                deal_id=deal_id,
            )
        ]

    async def _outbox_lag(self) -> list[Finding]:
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.min(NotificationOutbox.id),
                    sa.func.min(NotificationOutbox.next_attempt_at),
                ).where(
                    NotificationOutbox.status == OutboxStatus.PENDING,
                    NotificationOutbox.next_attempt_at < self._now - self._settings.worker_lag_alert,
                )
            )
        ).one()
        count, oldest_id, due = row
        if not count:
            return []
        return [
            Finding(
                "outbox",
                Severity.WARNING,
                f"Уведомления не отправляются: {count} шт. в очереди, старейшее ждёт "
                f"{_age(self._now - due)}. Проверьте воркер и токен бота.",
                key=f"outbox:{oldest_id}",
            )
        ]

    # ── housekeeping ───────────────────────────────────────────────────────
    async def remind_refund_addresses(self) -> int:
        """A refund without an address: remind the buyer once per interval (deduplicated)."""
        db_now = self._db_clock()
        interval = self._settings.refund_address_reminder_interval
        rows = await self._s.execute(
            sa.select(Payout.id, Payout.created_at, Deal.id, Deal.buyer_id)
            .join(Deal, Deal.id == Payout.deal_id)
            .where(
                Payout.status == PayoutStatus.AWAITING_ADDRESS,
                Payout.created_at <= db_now - interval,
                Deal.buyer_id.is_not(None),
            )
        )
        sent = 0
        for payout_id, created_at, deal_id, buyer_id in rows.all():
            if buyer_id is None:  # pragma: no cover - filtered by the query
                continue
            n = (db_now - created_at) // interval
            sent += await notifications.enqueue(
                self._s,
                user_id=buyer_id,
                kind=Notice.REFUND_ADDRESS_NEEDED,
                deal_id=deal_id,
                dedupe_key=f"refund-address:{payout_id}:{n}",
                now=self._now,
                payload={"payout_id": payout_id, "reminder": n},
            )
        return sent

    async def release_tags(self) -> int:
        """Unique-amount tags whose cooldown is over become available to new payment requests."""
        result = await self._s.execute(
            sa.update(PaymentRequest)
            .where(
                PaymentRequest.tag_reserved.is_(True),
                PaymentRequest.status != PaymentRequestStatus.ACTIVE,
                PaymentRequest.tag_release_at <= self._now,
            )
            .values(tag_reserved=False)
            .execution_options(synchronize_session=False)
        )
        released = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if released:
            log.info("payment_tags_released", count=released)
        return released

    async def alert(self, findings: Sequence[Finding]) -> int:
        if not findings:
            return 0
        admins = await notifications.admin_ids(self._s)
        queued = 0
        for finding in findings:
            digest = hashlib.sha256(finding.key.encode()).hexdigest()[:32]
            for admin_id in admins:
                queued += await notifications.enqueue(
                    self._s,
                    user_id=admin_id,
                    kind=Notice.ADMIN_RECONCILIATION,
                    deal_id=finding.deal_id,
                    dedupe_key=f"recon:{digest}:{admin_id}",
                    now=self._now,
                    payload={
                        "severity": finding.severity.value,
                        "check": finding.check,
                        "message": finding.message,
                        "payout_id": finding.payout_id,
                    },
                )
        return queued

    # ── helpers ────────────────────────────────────────────────────────────
    def _db_clock(self) -> datetime:
        if self._db_now is None:
            raise RuntimeError("Reconciler.run() reads the database clock first")
        return self._db_now

    def _formatter(self, asset: str) -> _Fmt:
        return _Fmt(decimals_for(self._chains, asset))


@dataclass(frozen=True)
class _Fmt:
    decimals: int

    def __call__(self, amount: int) -> str:
        return format_amount(amount, self.decimals)


def decimals_for(chains: ChainsConfig, asset: str) -> int:
    coin, _, network = asset.partition("@")
    if coin == Asset.USDT.value:
        return chains.spec(Network(network), include_disabled=True).decimals
    return NATIVE_DECIMALS.get(coin, 6)


def _wallet_label(wallet: str) -> str:
    return "балансе CryptoBot" if wallet == CRYPTOBOT_WALLET else f"кошельке {wallet}"


def _age(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{hours // 24} дн {hours % 24} ч"


# ── payout circuit breaker ───────────────────────────────────────────────────


async def pause_payouts(session: AsyncSession, wallet: str, *, reason: str, paused_by: int | None) -> bool:
    """Stop payouts from a wallet. False if they were already stopped."""
    result = await session.execute(
        pg_insert(PayoutPause)
        .values(wallet=wallet, reason=reason, paused_by=paused_by)
        .on_conflict_do_nothing(index_elements=[PayoutPause.wallet])
    )
    created = bool(result.rowcount)  # type: ignore[attr-defined]
    if created:
        log.warning("payouts_paused", wallet=wallet, paused_by=paused_by, reason=reason)
    return created


async def resume_payouts(session: AsyncSession, wallet: str) -> PayoutPause | None:
    """Lift the pause. Returns the removed pause (None if payouts were not stopped)."""
    pause = await session.get(PayoutPause, wallet, with_for_update=True)
    if pause is not None:
        await session.delete(pause)
        log.warning("payouts_resumed", wallet=wallet)
    return pause


# ── entry point ──────────────────────────────────────────────────────────────


async def reconcile(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    chains: ChainsConfig,
    *,
    sources: Sequence[BalanceSource],
    now: datetime,
) -> Report:
    """One full run: read external balances (outside any DB transaction), then check and record."""
    observed: list[ObservedBalance] = []
    errors: list[str] = []
    for source in sources:
        try:
            observed += await source.balances()
        except Exception as exc:  # one provider down must not stop the ledger checks
            log.warning("reconciliation_source_failed", source=source.name, error=repr(exc))
            errors.append(source.name)
    async with sessionmaker() as session, session.begin():
        locked = (await session.execute(sa.select(sa.func.pg_try_advisory_xact_lock(_LOCK_ID)))).scalar_one()
        if not locked:
            log.info("reconciliation_skipped", reason="another run in progress")
            return Report(skipped=True, source_errors=errors)
        report = await Reconciler(session, settings, chains, now=now).run(observed)
    report.source_errors = errors
    log.info(
        "reconciliation_done",
        wallets=len(report.runs),
        findings=len(report.findings),
        critical=len(report.critical),
        paused=report.paused,
        tags_released=report.tags_released,
        reminders=report.reminders_sent,
    )
    return report
