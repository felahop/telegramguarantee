"""payout_pauses (reconciliation circuit breaker), reconciliation_runs lookup index

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payout_pauses",
        sa.Column("wallet", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("paused_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("wallet", name=op.f("pk_payout_pauses")),
    )
    op.create_index(
        "ix_reconciliation_runs_wallet_created",
        "reconciliation_runs",
        ["wallet", "asset", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_runs_wallet_created", table_name="reconciliation_runs")
    op.drop_table("payout_pauses")
