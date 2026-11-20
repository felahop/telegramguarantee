"""Operator CLI against a real database."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.bot.storage import MemoryKV
from app.cli import build_parser, run_command
from app.db.models import AdminAudit, PayoutPause, User
from app.domain.enums import UserRole
from app.services.ledger import AccountCode, AccountRef
from app.services.reconciliation import pause_payouts
from app.worker.cryptobot import CryptoBotPayouts
from app.worker.periodic import reconciliation_job, run_if_due
from tests.integration.cb_world import CB, USDT, awaiting, confirmed_deal
from tests.integration.deal_world import BUYER

U = 10**6


async def cli(cb: CB, *argv: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await run_command(
        build_parser().parse_args(list(argv)),
        sessionmaker=cb.ctx.sessionmaker,
        settings=cb.ctx.settings,
        chains=cb.ctx.chains,
        cryptopay=cb.client,
        out=lines.append,
        clock=cb.clock,
    )
    return code, lines


async def test_record_funding_is_idempotent_by_ref(cb: CB) -> None:
    assert (await cli(cb, "record-funding", USDT, "cryptobot", "50", "topup-1"))[0] == 0
    code, out = await cli(cb, "record-funding", USDT, "cryptobot", "50", "topup-1")
    assert (code, out[0].startswith("already recorded")) == (0, True)  # booked once
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 50 * U
    code, out = await cli(cb, "record-funding", USDT, "cryptobot", "60", "topup-1")
    assert code == 1  # the same ref with another amount is a conflict, not a second top-up
    assert out[0].startswith("refused")
    async with cb.ctx.sessionmaker() as s:
        actions = list((await s.execute(sa.select(AdminAudit.action))).scalars())
    assert actions == ["cli:record-funding"]


async def test_funding_makes_reconciliation_match(cb: CB) -> None:
    cb.fake.balance_adjustment = Decimal("50")
    await cli(cb, "reconcile")
    cb.clock.advance(timedelta(minutes=15))
    assert (await cli(cb, "record-funding", USDT, "cryptobot", "50", "topup-2", "--note", "старт"))[0] == 0
    code, out = await cli(cb, "reconcile")
    assert code == 0
    assert out[0] == "cryptobot USDT@cryptobot: observed 50.00, ledger 50.00, diff 0.00"


async def test_withdrawal_never_touches_users_money(cb: CB) -> None:
    await confirmed_deal(cb)
    while await CryptoBotPayouts(cb.ctx, cb.client).run_once():
        pass
    # 2 USDT of earned fees are ours; a second deal's escrow is not
    _, invoice = await awaiting(cb)
    await cb.apply(cb.fake.pay(invoice))
    code, out = await cli(cb, "record-withdrawal", USDT, "cryptobot", "2.01", "out-1")
    assert code == 1
    assert "users are owed 102.00" in out[0]
    assert (await cli(cb, "record-withdrawal", USDT, "cryptobot", "2", "out-1"))[0] == 0
    assert await cb.balance(AccountRef.custody(USDT, "cryptobot")) == 102 * U
    # equity is credit-normal: the owner took out 2 USDT more than they put in
    assert await cb.balance(AccountRef(AccountCode.ADJUSTMENTS, USDT)) == -2 * U


async def test_reconcile_exit_code_and_resume(cb: CB) -> None:
    await confirmed_deal(cb)
    cb.fake.balance_adjustment = Decimal("-1")
    assert (await cli(cb, "reconcile"))[0] == 0
    cb.clock.advance(timedelta(minutes=15))
    code, out = await cli(cb, "reconcile")
    assert code == 2
    assert any(line.startswith("[critical] balance:") for line in out)
    async with cb.ctx.sessionmaker() as s:
        assert await s.get(PayoutPause, "cryptobot") is not None
    assert (await cli(cb, "resume-payouts", "cryptobot"))[0] == 0
    assert (await cli(cb, "resume-payouts", "cryptobot"))[0] == 1  # not paused any more


async def test_user_commands(cb: CB) -> None:
    assert (await cli(cb, "grant-role", str(BUYER), "arbitrator"))[0] == 0
    async with cb.ctx.sessionmaker() as s:
        user = await s.get(User, BUYER)
        assert user is not None
        assert user.role is UserRole.ARBITRATOR
    assert (await cli(cb, "reset-totp", "999999"))[0] == 1


async def test_manual_pause_survives_reconciliation(cb: CB) -> None:
    async with cb.ctx.sessionmaker() as s, s.begin():
        assert await pause_payouts(s, "cryptobot", reason="ручная проверка", paused_by=None)
        assert not await pause_payouts(s, "cryptobot", reason="ещё раз", paused_by=None)
    assert (await cli(cb, "reconcile"))[0] == 0
    async with cb.ctx.sessionmaker() as s:
        pause = await s.get(PayoutPause, "cryptobot")
        assert pause is not None
        assert pause.reason == "ручная проверка"


# ── periodic runner ──────────────────────────────────────────────────────────


async def test_periodic_job_runs_once_per_interval_across_replicas(cb: CB) -> None:
    now = [0.0]
    kv = MemoryKV(clock=lambda: now[0])
    replica_a = dataclasses.replace(cb.ctx, kv=kv)
    replica_b = dataclasses.replace(cb.ctx, kv=kv)
    ran: list[str] = []

    async def job_a() -> None:
        ran.append("a")

    async def job_b() -> None:
        ran.append("b")

    interval = timedelta(minutes=15)
    assert await run_if_due(replica_a, "reconcile", interval, job_a)
    assert not await run_if_due(replica_b, "reconcile", interval, job_b)
    now[0] += 14 * 60
    assert not await run_if_due(replica_a, "reconcile", interval, job_a)
    now[0] += 61
    assert await run_if_due(replica_b, "reconcile", interval, job_b)
    assert ran == ["a", "b"]


async def test_reconciliation_job_uses_the_context(cb: CB) -> None:
    ctx = dataclasses.replace(cb.ctx, cryptopay=cb.client)
    report = await reconciliation_job(ctx)()
    assert [r.wallet for r in report.runs] == ["cryptobot"]
    no_provider = await reconciliation_job(cb.ctx)()  # cb.ctx has no live client: ledger checks only
    assert no_provider.runs == []


def test_keystore_import_never_takes_the_key_as_an_argument(tmp_path: object) -> None:
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from eth_account import Account  # noqa: PLC0415

    from app.cli import keystore_import  # noqa: PLC0415

    target = Path(str(tmp_path)) / "hot.json"
    answers = iter(["0x" + "4c" * 32, "long enough password", "long enough password"])
    lines: list[str] = []
    assert keystore_import(str(target), read_secret=lambda _: next(answers), out=lines.append) == 0
    assert "4c4c4c" not in target.read_text()  # encrypted
    key = Account.decrypt(json.loads(target.read_text()), "long enough password")
    assert Account.from_key(key).address in lines[0]
    assert oct(target.stat().st_mode)[-3:] == "600"
    short = iter(["0x" + "4c" * 32, "short", "short"])
    assert (
        keystore_import(
            str(Path(str(tmp_path)) / "b.json"), read_secret=lambda _: next(short), out=lines.append
        )
        == 1
    )
