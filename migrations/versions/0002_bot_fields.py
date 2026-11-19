"""bot fields: users.started_at, deals.inline_message_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deals", sa.Column("inline_message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "inline_message_id")
    op.drop_column("users", "started_at")
