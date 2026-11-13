"""chat_turn_runs.last_prompt_tokens：最近一次真实调用的上下文占用。

Revision ID: p6e7f8a9b0c1
Revises: o5d6e7f8a9b0
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "p6e7f8a9b0c1"
down_revision = "o5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_turn_runs",
        sa.Column("last_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("chat_turn_runs", "last_prompt_tokens")
