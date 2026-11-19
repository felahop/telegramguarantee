"""Engine and session factory.

Convention: every service operation runs inside a transaction opened by the caller
(``async with session.begin(): ...``) so that several services compose atomically
(e.g. "payment matched" + "deal FUNDED" + ledger + outbox in one commit).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(url: str, *, pool_size: int = 10, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        echo=echo,
        connect_args={"server_settings": {"application_name": "escrow-bot"}},
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit (we render cards from them).
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
