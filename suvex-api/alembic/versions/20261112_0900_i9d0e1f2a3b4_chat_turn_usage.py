"""对话轮次 token 用量（v1.92）。

`chat_turn_runs` 加四个 token 计数列。此前对话侧只把 usage 解析出来就丢掉了
（`_call_model` 返回的 `LlmReply.usage` 没有落库），于是 Agent Hub / 会话
列表无法回答"这一轮花了多少"。

`usage_reported` 单独一列：接入点可能全程不给 usage，三个 0 并不代表 0 消耗。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "i9d0e1f2a3b4"
down_revision = "h8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        op.add_column(
            "chat_turn_runs",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "chat_turn_runs",
        sa.Column("usage_reported", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("chat_turn_runs", "usage_reported")
    op.drop_column("chat_turn_runs", "total_tokens")
    op.drop_column("chat_turn_runs", "completion_tokens")
    op.drop_column("chat_turn_runs", "prompt_tokens")
