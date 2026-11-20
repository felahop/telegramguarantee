"""Reconciliation in the admin panel: the balances screen, stopping and resuming payouts, alerts."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.bot.admin_keyboards import AdmAct, AdmCb
from app.bot.context import BotContext
from app.db.models import PayoutPause
from app.providers.cryptobot.client import CryptoPayClient
from app.services.reconciliation import balance_sources, reconcile
from tests.bot.conftest import ALICE
from tests.bot.harness import BotHarness
from tests.bot.test_admin_flow import ADMIN, code, make_admin
from tests.bot.test_bot_flows import press
from tests.bot.test_execution_flow import funded
from tests.bot.test_notifications import drain
from tests.cryptobot_fake import FakeCryptoPay
from tests.integration.deal_world import Clock


async def run(ctx: BotContext) -> None:
    await reconcile(
        ctx.sessionmaker,
        ctx.settings,
        ctx.chains,
        sources=balance_sources(ctx.chains, ctx.cryptopay),
        now=ctx.clock(),
    )


async def test_balances_screen_reconcile_pause_and_resume(
    paying_bot: BotHarness,
    ctx: BotContext,
    cryptopay: CryptoPayClient,
    fake: FakeCryptoPay,
    clock: Clock,
) -> None:
    bot = paying_bot
    ctx.cryptopay = cryptopay
    sessionmaker = ctx.sessionmaker
    await funded(bot, ctx, fake, sessionmaker)
    secret = await make_admin(bot, sessionmaker, clock)

    await bot.send(ADMIN, "/admin")
    await press(bot, ADMIN, "Балансы")
    screen = bot.last_text(ADMIN.id)
    assert "🧮 <b>Сверка</b>" in screen
    assert "ещё не проводилась" in screen

    await press(bot, ADMIN, "Сверить сейчас")
    report = bot.last_text(ADMIN.id)
    assert "Сверка выполнена" in report
    assert "Расхождений и зависших операций нет" in report

    await bot.send(ADMIN, "/admin")
    await press(bot, ADMIN, "Балансы")
    assert "CryptoBot: ✅ совпадает" in bot.last_text(ADMIN.id)

    await press(bot, ADMIN, "Остановить выплаты")
    assert "⏸ Выплаты остановлены." in bot.alerts()
    assert "остановлены</b> админом" in bot.last_text(ADMIN.id)
    async with sessionmaker() as s:
        pause = await s.get(PayoutPause, "cryptobot")
        assert pause is not None
        assert pause.paused_by == ADMIN.id
    await bot.send(ADMIN, "/admin")
    assert "Выплаты остановлены:</b> CryptoBot" in bot.last_text(ADMIN.id)

    await press(bot, ADMIN, "Балансы")
    await press(bot, ADMIN, "Возобновить выплаты")
    assert "Введите 6-значный код" in bot.last_text(ADMIN.id)
    await bot.send(ADMIN, code(secret, clock))
    assert "возобновлены" in bot.last_text(ADMIN.id)
    async with sessionmaker() as s:
        assert await s.get(PayoutPause, "cryptobot") is None


async def test_critical_alert_reaches_admins_with_a_button(
    paying_bot: BotHarness,
    ctx: BotContext,
    cryptopay: CryptoPayClient,
    fake: FakeCryptoPay,
    clock: Clock,
) -> None:
    bot = paying_bot
    ctx.cryptopay = cryptopay
    sessionmaker = ctx.sessionmaker
    await make_admin(bot, sessionmaker, clock)
    fake.balance_adjustment = Decimal("-5")  # someone drained the app balance with the API token
    await run(ctx)
    clock.advance(timedelta(minutes=15))
    await run(ctx)
    await drain(ctx, bot)
    shown = bot.last_for(ADMIN.id)
    assert shown is not None
    assert "🚨 Сверка: критично" in shown.text
    assert "Недостача на балансе CryptoBot" in shown.text
    assert shown.data("Открыть в админке") == AdmCb(a=AdmAct.BALANCES).pack()
    async with sessionmaker() as s:
        assert await s.get(PayoutPause, "cryptobot") is not None


async def test_resume_needs_an_admin(paying_bot: BotHarness, ctx: BotContext) -> None:
    bot = paying_bot
    await bot.send(ALICE, "/start")
    await bot.press(ALICE, AdmCb(a=AdmAct.RESUME, x="cryptobot").pack())
    await bot.press(ALICE, AdmCb(a=AdmAct.PAUSE, x="cryptobot").pack())
    async with ctx.sessionmaker() as s:
        assert await s.get(PayoutPause, "cryptobot") is None
