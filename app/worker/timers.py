"""Deal timers loop: offers/payments expiring, deadline notices, reminders, auto-disputes, SLA alerts."""

from __future__ import annotations

import asyncio

from app.bot.context import BotContext
from app.domain.errors import DomainError
from app.logging import get_logger

log = get_logger(__name__)
POLL_SECONDS = 15.0


async def run_due_timers(ctx: BotContext, *, limit: int = 100) -> int:
    """Process every due timer once; each deal in its own transaction. Returns how many ran."""
    async with ctx.uow() as uow:
        deal_ids = await uow.deals.due_deal_ids(limit)
    processed = 0
    for deal_id in deal_ids:
        try:
            async with ctx.uow() as uow:
                processed += int(await uow.deals.process_timer(deal_id))
        except DomainError as exc:
            log.warning("timer_domain_error", deal_id=deal_id, code=exc.code, message=exc.message)
        except Exception as exc:
            log.error("timer_failed", deal_id=deal_id, error=repr(exc), exc_info=exc)
    return processed


async def run_timers(ctx: BotContext, stop: asyncio.Event) -> None:
    log.info("timers_started")
    while not stop.is_set():
        try:
            await run_due_timers(ctx)
        except Exception as exc:
            log.error("timers_loop_error", error=repr(exc), exc_info=exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            continue
