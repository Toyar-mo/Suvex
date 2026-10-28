"""chat: 会话加 `pinned_at` —— 置顶（v1.15）

## 这个迁移在加什么

侧栏会话条目要支持「置顶此对话」。置顶的排在列表最前，其余按最近更新。
一列 `pinned_at`（可空时间戳）就够：`NULL` = 没置顶。

## 为什么是时间戳而不是 `pinned BOOLEAN`

置顶**多条**时需要它们之间的顺序。用布尔值就只能再拿 `updated_at` 兜底，
而 `updated_at` 会因为"又聊了两句"而变 —— 用户置顶的三条会自己换位置。
存时刻则「最近置顶的在上」是确定的，且不需要第二列。取消置顶就是写回
`NULL`，语义单一。

## 为什么不需要索引

会话列表的 WHERE 只有 `created_by`（+ 归档条件），行数是"某个人的会话数"，
量级在几十到几百。排序字段加索引对这点行数没有意义，而多一个索引就多
一处必须与模型声明同步的地方（本模块已因索引漂移吃过一次 `alembic check`
永远想生成 diff 的亏，见 `models.ChatSession.__table_args__` 的注释）。

## 兼容性

可空、默认 NULL。存量会话一律视为未置顶，不需要数据回填。
`ChatSessionOut.pinned` 由模型上的 `@property` 从这一列派生，不额外存。

## 回滚

直接 drop 列。回滚后置顶信息丢失（无法从别处恢复），但会话本身与消息
不受影响 —— 这一列不参与任何消息写入路径。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c4d9e1a72b38"
down_revision = "b71c4d9e83a5"
branch_labels = None
depends_on = None

TABLE = "chat_sessions"
COLUMN = "pinned_at"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
