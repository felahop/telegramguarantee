"""Periodic jobs that run at most once per interval across all worker replicas.

Every replica wakes up each ``tick`` and tries to take a KV lock whose TTL equals the job's
interval; only the replica that got it runs the job. The lock is never released early, so the
interval is the spacing between runs whichever replica ran last and however long the job took.
Losing the KV state (a Redis restart) can at worst run a job twice: every job here is idempotent,
and reconciliation additionally holds a PostgreSQL advisory lock while it runs.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta

from app.bot.context import BotContext
from app.logging import get_logger
from app.services.reconciliation import Report, balance_sources, reconcile

log = get_logger(__name__)

TICK_SECONDS = 30.0
Job = Callable[[], Awaitable[object]]


async def run_if_due(ctx: BotContext, name: str, interval: timedelta, job: Job) -> bool:
    """Run ``job`` if no replica ran it within ``interval``. Returns whether it ran here."""
    owner = f"{socket.gethostname()}:{os.getpid()}"
    if not await ctx.kv.set_nx(f"periodic:{name}", owner, ttl=max(int(interval.total_seconds()), 1)):
        return False
    await job()
    return True


async def run_periodic(
    ctx: BotContext,
    name: str,
    interval: timedelta,
    job: Job,
    stop: asyncio.Event,
    *,
    tick: float = TICK_SECONDS,
) -> None:
    log.info("periodic_started", job=name, interval_s=int(interval.total_seconds()))
    while not stop.is_set():
        try:
            await run_if_due(ctx, name, interval, job)
        except Exception as exc:  # the loop must survive a failing run (DB or Redis hiccup)
            log.error("periodic_job_failed", job=name, error=repr(exc), exc_info=exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(tick, interval.total_seconds()))
        except TimeoutError:
            continue


def reconciliation_job(ctx: BotContext) -> Callable[[], Awaitable[Report]]:
    sources = balance_sources(ctx.chains, ctx.cryptopay, ctx.chain_adapters.values())

    async def job() -> Report:
        return await reconcile(ctx.sessionmaker, ctx.settings, ctx.chains, sources=sources, now=ctx.clock())

    return job
