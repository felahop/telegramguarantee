"""The Alembic migrations build exactly the schema the ORM models describe (no drift)."""

from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import app.db.models  # noqa: F401 - registers every table
from app.db.base import Base


async def test_no_drift_between_models_and_migrations(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
