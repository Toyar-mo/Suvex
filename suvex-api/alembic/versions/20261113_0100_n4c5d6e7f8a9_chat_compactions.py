"""chat_compactions：对话上下文压缩边界。

Revision ID: n4c5d6e7f8a9
Revises: m3b4c5d6e7f8
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "n4c5d6e7f8a9"
down_revision = "m3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_compactions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("first_kept_seq", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_chat_compactions_session_created",
        "chat_compactions",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_compactions_session_created", table_name="chat_compactions")
    op.drop_table("chat_compactions")
