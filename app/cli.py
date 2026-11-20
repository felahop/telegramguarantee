"""Operator CLI (``python -m app.cli``): things that must not be possible from Telegram alone.

grant-role <user_id> <user|arbitrator|admin>        change a user's role
reset-totp <user_id>                                drop an admin's TOTP (they re-enroll with /totp)
record-funding <asset> <wallet> <amount> <ref>      the operator topped up a wallet, e.g.
                                                    ``record-funding USDT@cryptobot cryptobot 50 topup-0612``
record-withdrawal <asset> <wallet> <amount> <ref>   the operator took money out (earned fees) or
                                                    writes off a confirmed loss; never below what
                                                    users are owed
reconcile                                           run reconciliation now; exit code 2 on critical findings
resume-payouts <wallet>                             lift a payout pause (``cryptobot`` or an address)

Every command is written to ``admin_audit`` with admin_id 0. ``ref`` makes funding/withdrawal
idempotent: running the same command twice books it once.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import ChainsConfig, Settings, get_chains, get_settings
from app.db.models import AdminAudit, LedgerAccount, LedgerTransaction, User
from app.db.session import create_engine, create_sessionmaker
from app.domain.enums import UserRole
from app.domain.errors import DomainError
from app.domain.money import format_amount, to_minor
from app.providers.cryptobot.client import CryptoPayClient
from app.services import ledger as postings
from app.services.deals import utcnow
from app.services.ledger import AccountCode, Ledger
from app.services.reconciliation import balance_sources, decimals_for, reconcile, resume_payouts

Out = Callable[[str], None]
_LIABILITIES = [
    AccountCode.ESCROW.value,
    AccountCode.PAYABLE.value,
    AccountCode.UNIDENTIFIED.value,
    AccountCode.PAYABLE_UNIDENTIFIED.value,
]


def _print(text: str) -> None:
    print(text)  # noqa: T201 - a CLI


def _audit(
    session: AsyncSession, action: str, target_type: str, target_id: int | None, **payload: object
) -> None:
    session.add(
        AdminAudit(
            admin_id=0, action=f"cli:{action}", target_type=target_type, target_id=target_id, payload=payload
        )
    )


async def _user_command(session: AsyncSession, args: argparse.Namespace, out: Out) -> int:
    user = await session.get(User, args.user_id, with_for_update=True)
    if user is None:
        out(f"user {args.user_id} not found (they must /start the bot first)")
        return 1
    if args.command == "grant-role":
        user.role = UserRole(args.role)
        if user.role is UserRole.USER:
            user.totp_secret_enc = None
            user.totp_confirmed_at = None
    else:
        user.totp_secret_enc = None
        user.totp_confirmed_at = None
    _audit(session, args.command, "user", user.id, role=getattr(args, "role", None))
    out(f"ok: {args.command} {args.user_id}")
    return 0


async def _owed(session: AsyncSession, asset: str) -> tuple[int, int]:
    """(held on all wallets of the asset, owed to users) in minor units."""
    rows = await session.execute(
        sa.select(LedgerAccount.code, sa.func.sum(LedgerAccount.balance))
        .where(
            LedgerAccount.asset == asset, LedgerAccount.code.in_([AccountCode.CUSTODY.value, *_LIABILITIES])
        )
        .group_by(LedgerAccount.code)
    )
    held = owed = 0
    for code, total in rows.all():
        if code == AccountCode.CUSTODY.value:
            held += int(total)
        else:
            owed -= int(total)
    return held, owed


async def _money_command(
    session: AsyncSession, args: argparse.Namespace, chains: ChainsConfig, out: Out
) -> int:
    decimals = decimals_for(chains, args.asset)
    amount = to_minor(Decimal(args.amount), decimals)
    if amount <= 0:
        out("amount must be positive")
        return 1
    funding = args.command == "record-funding"
    if not funding:
        # Serialize with other withdrawals of the asset and check that users stay covered.
        await session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext(f"withdraw:{args.asset}")))
        )
        held, owed = await _owed(session, args.asset)
        if held - amount < owed:
            out(
                f"refused: after the withdrawal wallets would hold {format_amount(held - amount, decimals)}, "
                f"but users are owed {format_amount(owed, decimals)}"
            )
            return 1
    entries = (
        postings.operator_funding(asset=args.asset, wallet=args.wallet, amount=amount)
        if funding
        else postings.operator_withdrawal(asset=args.asset, wallet=args.wallet, amount=amount)
    )
    kind = "operator_funding" if funding else "operator_withdrawal"
    key = f"{kind}:{args.ref}"
    replay = await session.scalar(
        sa.select(LedgerTransaction.id).where(LedgerTransaction.idempotency_key == key)
    )
    txn = await Ledger(session).post(  # a replay with other postings raises IdempotencyConflict
        idempotency_key=key,
        kind=kind,
        postings=entries,
        meta={"wallet": args.wallet, "ref": args.ref, "note": args.note or ""},
    )
    if replay is not None:
        out(f"already recorded: {kind} ref {args.ref} (transaction #{txn.id})")
        return 0
    _audit(
        session,
        args.command,
        "ledger_transaction",
        txn.id,
        asset=args.asset,
        wallet=args.wallet,
        amount=amount,
        ref=args.ref,
    )
    out(f"ok: {kind} {format_amount(amount, decimals)} {args.asset} ({args.wallet}), ref {args.ref}")
    return 0


async def _reconcile_command(  # noqa: PLR0917 - internal, called once
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    chains: ChainsConfig,
    cryptopay: CryptoPayClient | None,
    now: datetime,
    out: Out,
) -> int:
    report = await reconcile(
        sessionmaker, settings, chains, sources=balance_sources(chains, cryptopay), now=now
    )
    if report.skipped:
        out("another reconciliation is running, try again in a minute")
        return 1
    for run in report.runs:
        d = decimals_for(chains, run.asset)
        observed, ledger = format_amount(run.chain_balance or 0, d), format_amount(run.ledger_balance or 0, d)
        diff = format_amount(run.diff or 0, d)
        out(f"{run.wallet} {run.asset}: observed {observed}, ledger {ledger}, diff {diff}")
    for name in report.source_errors:
        out(f"source unavailable: {name}")
    for finding in report.findings:
        out(f"[{finding.severity.value}] {finding.check}: {finding.message}")
    critical = len(report.critical)
    out(f"findings: {len(report.findings)}, critical: {critical}, tags released: {report.tags_released}")
    return 2 if report.critical else 0


async def _resume_command(session: AsyncSession, args: argparse.Namespace, out: Out) -> int:
    pause = await resume_payouts(session, args.wallet)
    if pause is None:
        out(f"payouts from {args.wallet} are not paused")
        return 1
    _audit(session, args.command, "wallet", None, wallet=args.wallet, paused_reason=pause.reason[:300])
    out(f"ok: payouts from {args.wallet} resumed (were paused: {pause.reason})")
    return 0


async def run_command(
    args: argparse.Namespace,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    chains: ChainsConfig,
    cryptopay: CryptoPayClient | None = None,
    out: Out = _print,
    clock: Callable[[], datetime] = utcnow,
) -> int:
    """Exit code: 0 ok, 1 refused/not found, 2 reconciliation found critical problems."""
    if args.command == "reconcile":
        return await _reconcile_command(sessionmaker, settings, chains, cryptopay, clock(), out)
    try:
        async with sessionmaker() as session, session.begin():
            if args.command in ("grant-role", "reset-totp"):
                return await _user_command(session, args, out)
            if args.command in ("record-funding", "record-withdrawal"):
                return await _money_command(session, args, chains, out)
            if args.command == "resume-payouts":
                return await _resume_command(session, args, out)
    except DomainError as exc:
        out(f"refused: {exc.message} {exc.context}")
        return 1
    raise ValueError(f"unknown command {args.command!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-role")
    grant.add_argument("user_id", type=int)
    grant.add_argument("role", choices=[r.value for r in UserRole])
    reset = sub.add_parser("reset-totp")
    reset.add_argument("user_id", type=int)
    for name in ("record-funding", "record-withdrawal"):
        money = sub.add_parser(name)
        money.add_argument("asset", help="ledger asset code, e.g. USDT@cryptobot or BNB@bsc")
        money.add_argument("wallet", help="cryptobot or a wallet address")
        money.add_argument("amount", help="human amount, e.g. 50 or 12.5")
        money.add_argument("ref", help="unique reference (tx hash, top-up id): makes the command idempotent")
        money.add_argument("--note", default="")
    sub.add_parser("reconcile")
    resume = sub.add_parser("resume-payouts")
    resume.add_argument("wallet")
    return parser


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_size=1)
    cryptopay = (
        CryptoPayClient(settings.cryptobot_api_token, testnet=settings.cryptobot_testnet)
        if args.command == "reconcile" and settings.cryptobot_api_token.get_secret_value()
        else None
    )
    try:
        return await run_command(
            args,
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            chains=get_chains(),
            cryptopay=cryptopay,
        )
    finally:
        if cryptopay is not None:
            await cryptopay.close()
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_main(build_parser().parse_args())))


if __name__ == "__main__":
    main()
