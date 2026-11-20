"""incoming_transfers.position (scan order of the ChainWatcher)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("incoming_transfers", sa.Column("position", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("incoming_transfers", "position")
