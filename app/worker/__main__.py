"""Background worker (``python -m app.worker``): outbox notifications, deal timers, CryptoBot
invoice polling and payouts, a ChainWatcher per enabled on-chain network, and reconciliation
(balances, ledger invariants, stuck work).

Several replicas may run: queue jobs claim their rows with ``FOR UPDATE SKIP LOCKED``; periodic
jobs run once per interval across replicas (a KV lock plus a PostgreSQL advisory lock).
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Coroutine
from typing import Any

from app.bot.context import BotContext
from app.bot.factory import build_bot
from app.chains.adapters.evm import EvmAdapter
from app.chains.adapters.tron import TronAdapter
from app.chains.registry import build_adapters
from app.chains.watcher import ChainWatcher, run_watcher
from app.config import get_settings
from app.domain.enums import Network
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime
from app.services.tx_claims import process_claims
from app.signer.remote import RemoteSigner
from app.signer.signer import Signer, SigningService
from app.worker.cryptobot import run_payouts, run_poller
from app.worker.onchain_payouts import EvmPayouts, OnchainPayouts, TronPayouts, run_onchain_payouts
from app.worker.outbox import OutboxSender
from app.worker.periodic import reconciliation_job, run_periodic
from app.worker.timers import run_timers

log = get_logger(__name__)
CLAIMS_POLL_SECONDS = 10.0


async def _signer(ctx: BotContext) -> SigningService:
    settings = ctx.settings
    if settings.signer_url:
        remote = RemoteSigner(settings.signer_url, settings.signer_api_secret.get_secret_value().encode())
        await remote.load()  # which hot wallets it holds; fails loudly if the secret is wrong
        return remote
    if settings.environment == "prod":
        log.warning("signer_in_process", hint="run python -m app.signer separately and set SIGNER_URL")
    return Signer.from_keystore(settings, ctx.chains, clock=ctx.clock)


async def _chain_jobs(ctx: BotContext, stop: asyncio.Event) -> list[Coroutine[Any, Any, None]]:
    """A watcher per enabled network, and a payout executor where the signer holds the hot wallet."""
    settings = ctx.settings
    signer = await _signer(ctx) if ctx.chain_adapters else None
    jobs: list[Coroutine[Any, Any, None]] = []
    watchers: dict[Network, ChainWatcher] = {}
    for network, adapter in ctx.chain_adapters.items():
        executor: OnchainPayouts | None = None
        if isinstance(adapter, EvmAdapter):
            await adapter.check_contract()  # wrong decimals = refuse to start, never mis-credit
            expected = ctx.chains.networks[network].chain_id
            if expected is None or await adapter.chain_id() != expected:
                raise RuntimeError(f"{network}: the RPC chain id differs from chain_id in chains.yaml")
            if signer is not None and signer.has_key(adapter.addresses[0]):
                executor = EvmPayouts(ctx, adapter, signer)
        elif isinstance(adapter, TronAdapter) and signer is not None and signer.has_key(adapter.addresses[0]):
            executor = TronPayouts(ctx, adapter, signer)
        if executor is not None:
            jobs.append(run_onchain_payouts(executor, stop))
        else:
            log.warning("onchain_payouts_disabled", network=network.value, reason="no signer key or executor")
        watcher = ChainWatcher(
            ctx.sessionmaker, settings, ctx.chains, adapter, clock=ctx.clock, gateway=ctx.gateway
        )
        watchers[network] = watcher
        jobs.append(run_watcher(watcher, ctx.chains.networks[network].poll_interval_seconds, stop))
    if watchers:
        jobs.append(run_claims(ctx, watchers, stop))
    return jobs


async def run_claims(ctx: BotContext, watchers: dict[Network, ChainWatcher], stop: asyncio.Event) -> None:
    """Buyers' "here is my transaction hash" requests, looked up by the worker (it has the RPCs)."""
    while not stop.is_set():
        try:
            await process_claims(ctx.sessionmaker, ctx.chain_adapters, watchers, clock=ctx.clock)
        except Exception as exc:
            log.warning("tx_claims_loop_error", error=repr(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLAIMS_POLL_SECONDS)
        except TimeoutError:
            continue


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
    jobs += await _chain_jobs(ctx, stop)
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
