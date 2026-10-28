"""chat: `chat_turn_runs.truncations_used` —— 把"被切断"的续跑额度也持久化（v1.21）

## 这个迁移在加什么

`chat_turn_runs` 上一列 `truncations_used`：这一轮里有多少次纠偏是
**回复被 `max_tokens` 切断**引起的。

## 为什么不能从现有列推算

`nudges_used` 记的是纠偏总次数（格式错 + 被切断）。截断额度是否用完，
需要知道其中"被切断"占了几次；而 `finish_reason` 只在模型回复的那一刻
存在，没有落库。所以只能单存一列。

## 不加会怎样

后台接手（`app.chat.resumer`）从 `nudges_used` 恢复上下文。若截断额度
没有持久化，每次自动接手都把它重置成 0 —— 一条反复撞 `max_tokens` 的
长回复可以无限续下去，每 90 秒烧一次模型调用，而用户在界面上看到的是
同一条消息反复刷新。

## 默认值

`server_default=sa.text("0")`：存量行（v1.19 起产生）确实一次截断续跑
也没有过，0 是它们的真实值，不是占位。

## 兼容性

只加列，不改不删。`nullable=False` + `server_default` 让 PostgreSQL 在
存量行上直接填 0，不需要两步迁移。

Revision ID: e8b4c1f60a72
Revises: d5e2f8b31c47
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8b4c1f60a72"
down_revision = "d5e2f8b31c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_turn_runs",
        sa.Column(
            "truncations_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_turn_runs", "truncations_used")
