"""provider_accounts.context_window_tokens：自定义模型上下文窗口。

Revision ID: o5d6e7f8a9b0
Revises: n4c5d6e7f8a9
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "o5d6e7f8a9b0"
down_revision = "n4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_accounts",
        sa.Column("context_window_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_accounts", "context_window_tokens")
