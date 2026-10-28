"""chat: `chat_turn_runs.tool_nudges_used` —— 「跑过工具」的纠偏额度也持久化（v1.23）

## 这个迁移在加什么

`chat_turn_runs` 上一列 `tool_nudges_used`：这一轮里有多少次纠偏是
「本轮**跑过工具**、模型却只写了一句人话就收尾」引起的。

## 为什么不能复用 `nudges_used`

实测线上会话 `chat_01M61DWC71W8989RRMW6DZS9SP`（33 条消息）：
`nudges_used=2 / truncations_used=0 / tools_used=2`，界面停在
「Let me see the rest of the main function.」—— 模型刚跑完反汇编，
写了一句"我看看剩下的"，没写 ACTION。

那两次纠偏额度**是被前面一条空回复吃掉的**（`seq 18` 内容长度 0），
而 `should_nudge` 里 `nudges_used >= max_nudges` 排在 `tools_used > 0`
**之前**，于是最硬的结构信号根本没机会生效。

## 为什么必须持久化

后台接手（`app.chat.resumer`）从这一行恢复上下文。若这个计数不落库，
每次自动接手都把它重置成 0 —— 一条反复"写完一句就停"的长任务可以
无限续下去。

## 默认值

`server_default=sa.text("0")`：存量行确实一次这样的纠偏也没有过。

Revision ID: f7a2d5c91b36
Revises: e8b4c1f60a72
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a2d5c91b36"
down_revision = "e8b4c1f60a72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_turn_runs",
        sa.Column(
            "tool_nudges_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_turn_runs", "tool_nudges_used")
