"""Runtime dependencies shared by handlers (injected as ``ctx`` into every handler)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.storage import KeyValueStore
from app.config import AssetSpec, ChainsConfig, Settings
from app.domain.enums import Network
from app.services.deals import DealService, PaymentGateway, utcnow


@dataclass
class UnitOfWork:
    session: AsyncSession
    deals: DealService


@dataclass
class BotContext:
    settings: Settings
    chains: ChainsConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    kv: KeyValueStore
    clock: Callable[[], datetime] = field(default=utcnow)
    gateway: PaymentGateway | None = None

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[UnitOfWork]:
        """One DB transaction. Telegram calls should be made *after* leaving the block, so that the
        user is never told about a change that was rolled back."""
        async with self.sessionmaker() as session, session.begin():
            yield UnitOfWork(
                session,
                DealService(session, self.settings, self.chains, gateway=self.gateway, clock=self.clock),
            )

    @property
    def bot_username(self) -> str:
        return self.settings.bot_username

    def enabled_specs(self) -> list[AssetSpec]:
        return [self.chains.spec(n) for n in self.chains.enabled_networks()]

    def spec(self, network: Network) -> AssetSpec:
        return self.chains.spec(network, include_disabled=True)

    def default_network(self, preferred: Network | None) -> Network:
        enabled = self.chains.enabled_networks()
        if preferred in enabled:
            return preferred
        return enabled[0]
