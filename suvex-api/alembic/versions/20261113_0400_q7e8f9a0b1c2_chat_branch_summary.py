"""chat_sessions.branch_summary：分叉时生成的分支摘要。

Revision ID: q7e8f9a0b1c2
Revises: p6e7f8a9b0c1
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "q7e8f9a0b1c2"
down_revision = "p6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("branch_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "branch_summary")
