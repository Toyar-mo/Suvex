"""对话工具卡耗时（v1.92）。

`chat_messages.duration_ms` —— 工具执行本身花了多久。

## 为什么不在前端按相邻消息时间差算

相邻两条消息的 `created_at` 差包含工具执行 + 模型下一段输出 + 落库排队。
把它当"工具耗时"会把 0.2 秒的 `pwd` 显示成几十秒，而失真没有任何信号。
计时器包住 `_execute_tool`，才是同一件事。

## 为什么可空

历史消息没有这个值。NULL = 不知道，界面不显示耗时；写成 0 会被读成
"瞬间完成"，那是对历史数据的编造。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "g7b8c9d0e1f2"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("duration_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "duration_ms")
