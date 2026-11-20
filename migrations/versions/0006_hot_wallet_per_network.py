"""hot_wallets unique per (network, address): one EVM key serves BSC and Ethereum

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20 16:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_hot_wallets_address", "hot_wallets", type_="unique")
    op.create_unique_constraint("uq_hot_wallets_network_address", "hot_wallets", ["network", "address"])


def downgrade() -> None:
    op.drop_constraint("uq_hot_wallets_network_address", "hot_wallets", type_="unique")
    op.create_unique_constraint("uq_hot_wallets_address", "hot_wallets", ["address"])
