"""tx_claims: 'submitted' status, result text, queue index

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-20 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("auto_matched", "pending_review", "approved", "rejected")
NEW = ("submitted", *OLD)


def _check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tx_claims_status"), "tx_claims", type_="check")
    op.create_check_constraint(op.f("ck_tx_claims_status"), "tx_claims", _check(NEW))
    op.add_column("tx_claims", sa.Column("result", sa.Text(), nullable=True))
    op.create_index(
        "ix_tx_claims_submitted", "tx_claims", ["created_at"], postgresql_where=sa.text("status = 'submitted'")
    )


def downgrade() -> None:
    op.drop_index("ix_tx_claims_submitted", table_name="tx_claims")
    op.drop_column("tx_claims", "result")
    op.execute("DELETE FROM tx_claims WHERE status = 'submitted'")
    op.drop_constraint(op.f("ck_tx_claims_status"), "tx_claims", type_="check")
    op.create_check_constraint(op.f("ck_tx_claims_status"), "tx_claims", _check(OLD))
