"""Background worker (``python -m app.worker``): outbox notifications, deal timers, CryptoBot
invoice polling and payouts, a ChainWatcher per enabled on-chain network, and reconciliation
(balances, ledger invariants, stuck work).

Several replicas may run: queue jobs claim their rows with ``FOR UPDATE SKIP LOCKED``; periodic
jobs run once per interval across replicas (a KV lock plus a PostgreSQL advisory lock).
"""

from __future__ import annotations

import asyncio
import signal

from app.bot.factory import build_bot
from app.chains.adapters.evm import EvmAdapter
from app.chains.registry import build_adapters
from app.chains.watcher import ChainWatcher, run_watcher
from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime
from app.signer.signer import Signer
from app.worker.cryptobot import run_payouts, run_poller
from app.worker.onchain_payouts import EvmPayouts, run_evm_payouts
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
    ctx = runtime.ctx
    ctx.chain_adapters = build_adapters(ctx.chains, settings)  # before the jobs that read them
    jobs = [
        OutboxSender(ctx, bot).run(stop),
        run_timers(ctx, stop),
        run_periodic(ctx, "reconcile", settings.reconcile_interval, reconciliation_job(ctx), stop),
    ]
    signer = Signer.from_keystore(settings, ctx.chains, clock=ctx.clock) if ctx.chain_adapters else None
    for network, adapter in ctx.chain_adapters.items():
        if isinstance(adapter, EvmAdapter):
            await adapter.check_contract()  # wrong decimals = refuse to start, never mis-credit
            expected = ctx.chains.networks[network].chain_id
            if expected is None or await adapter.chain_id() != expected:
                raise RuntimeError(f"{network}: the RPC chain id differs from chain_id in chains.yaml")
            if signer is not None and signer.has_key(adapter.addresses[0]):
                jobs.append(run_evm_payouts(EvmPayouts(ctx, adapter, signer), stop))
            else:
                log.warning(
                    "onchain_payouts_disabled", network=network.value, reason="no key for the hot wallet"
                )
        watcher = ChainWatcher(
            ctx.sessionmaker, settings, ctx.chains, adapter, clock=ctx.clock, gateway=ctx.gateway
        )
        jobs.append(run_watcher(watcher, ctx.chains.networks[network].poll_interval_seconds, stop))
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
        for adapter in ctx.chain_adapters.values():
            await adapter.close()
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
