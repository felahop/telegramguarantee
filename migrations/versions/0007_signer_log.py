"""signer_log: the signer's own record of signatures

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NETWORKS = ("cryptobot", "bsc", "eth", "tron", "ton", "sol")


def upgrade() -> None:
    op.create_table(
        "signer_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payout_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "network",
            sa.Enum(*NETWORKS, name="network", native_enum=False, create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("wallet", sa.Text(), nullable=False),
        sa.Column("tx_hash", sa.Text(), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payout_id"], ["payouts.id"], name=op.f("fk_signer_log_payout_id_payouts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signer_log")),
    )
    op.create_index(op.f("ix_signer_log_payout_id"), "signer_log", ["payout_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_signer_log_payout_id"), table_name="signer_log")
    op.drop_table("signer_log")
