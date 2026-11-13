"""chat_compactions.short_summary：OMP 短摘要（PR 风格）。

Revision ID: r8f9a0b1c2d3
Revises: q7e8f9a0b1c2
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "r8f9a0b1c2d3"
down_revision = "q7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_compactions",
        sa.Column("short_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_compactions", "short_summary")
