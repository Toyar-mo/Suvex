"""会话分叉（v1.92）。

`chat_sessions.forked_from_session_id` / `forked_from_seq`：从某条消息处
复制一段历史，另开一条时间线。与 `parent_session_id`（子代理的父子关系）
刻意分开 —— 分支不是委派，混用会让 Agent Hub 把用户分支当成子代理。

不建外键：源会话被彻底删除后，分支仍是可继续的对话，只是来源成了历史事实。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "h8c9d0e1f2a3"
down_revision = "g7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("forked_from_session_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("forked_from_seq", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "forked_from_seq")
    op.drop_column("chat_sessions", "forked_from_session_id")
