"""Background worker (``python -m app.worker``): outbox notifications, deal timers, CryptoBot
invoice polling and payouts, and reconciliation (balances, ledger invariants, stuck work).

Several replicas may run: queue jobs claim their rows with ``FOR UPDATE SKIP LOCKED``; periodic
jobs run once per interval across replicas (a KV lock plus a PostgreSQL advisory lock).
"""

from __future__ import annotations

import asyncio
import signal

from app.bot.factory import build_bot
from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime
from app.worker.cryptobot import run_payouts, run_poller
from app.worker.outbox import OutboxSender
from app.worker.periodic import reconciliation_job, run_periodic
from app.worker.timers import run_timers

log = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    runtime = await build_runtime(settings)
    bot = build_bot(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    jobs = [
        OutboxSender(runtime.ctx, bot).run(stop),
        run_timers(runtime.ctx, stop),
        run_periodic(
            runtime.ctx, "reconcile", settings.reconcile_interval, reconciliation_job(runtime.ctx), stop
        ),
    ]
    if runtime.cryptopay is not None:
        jobs += [
            run_poller(runtime.ctx, runtime.cryptopay, stop),
            run_payouts(runtime.ctx, runtime.cryptopay, stop),
        ]
    else:
        log.warning("cryptobot_disabled", reason="CRYPTOBOT_API_TOKEN is empty or the network is disabled")
    try:
        await asyncio.gather(*jobs)
    finally:
        log.info("worker_stopping")
        await bot.session.close()
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
