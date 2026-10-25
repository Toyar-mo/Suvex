"""`chat_sessions` / `chat_messages` 两张表：对话页（v0.93）。

## 为什么加这两张表

用户要一个"能在对话里解题、建扫描、改技能库、加 MCP 工具"的页面
（m09559）。之前平台只有**表单式**入口：建扫描要填六个字段、起解题要点
三次、改技能要去技能页。对话页把这些压成一句话 —— 而一张"说过什么、
做了什么"的表是这个页面的**唯一持久化形态**：
没有它，刷新一次对话就没了，而"上周我是怎么定位到这个漏洞的"
这件事就再也回答不了。

## 为什么是两张表而不是一张

`chat_sessions` 是**容器**（标题、归属、归档），`chat_messages` 是**内容**。

合一张表会让每个会话的标题/归属在每一行上重复，而更麻烦的是
**归档**：归档一个会话要更新它的全部行，中途失败会留下半个归档的会话
（一部分消息还在默认列表里）。分表之后归档是一次 UPDATE。

## `seq` 为什么必须有

`created_at` **不足以**排序：同一次工具调用里，"assistant 说要做什么"
和"tool 的结果"可能落在同一毫秒（Python 里 `now_utc()` 的精度足够高，
但 PostgreSQL 的 `timestamptz` 默认精度是微秒，而两次 INSERT 完全可能
落进同一微秒）。排序一旦不稳定，界面上会出现「结果跑到了调用前面」。

所以用 `(session_id, seq)` 唯一约束在**数据库层**保证顺序存在且唯一，
而不是靠应用层"小心一点"。这也是本项目在 `finding_timeline` 上用过的
同一手法。

## `role` 的 CHECK 约束为什么写死三个值

`user` / `assistant` / `tool`。写死而不是随便放过，是因为**多一个角色
会让喂给模型的上下文拼装出错**（`engine._render_history` 对每个角色
都有明确映射，遇到第四个会掉进 `else` 分支当成 user）。

## 枚举值抄一遍而不 import 模型

迁移是**历史快照**，必须永远生成当初那张表。import `app.chat.models`
会让将来改模型时这个迁移悄悄生成一个不一样的表 —— alembic 最经典的坑，
本仓库其它迁移的 docstring 里也反复写了这一条。

Revision ID: d4e8b2f6a1c9
Revises: c7b1e4a90f38
Create Date: 2026-10-25 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4e8b2f6a1c9"
down_revision = "c7b1e4a90f38"
branch_labels = None
depends_on = None

#: ⚠️ 与 `app/chat/models.py::CHAT_ROLES` 必须逐字一致。
_CHAT_ROLES = ("user", "assistant", "tool")

_TITLE_CHARS = 200


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=_TITLE_CHARS), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        # 软归档：NULL = 在默认列表里。
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # 冗余的消息数（列表页要显示，避免 N+1 次 COUNT）。
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    # 列表页的唯一热查询：某人的会话，按更新时间倒序。
    op.create_index(
        "ix_chat_sessions_owner_updated",
        "chat_sessions",
        ["created_by", "updated_at"],
    )
    op.create_index("ix_chat_sessions_archived", "chat_sessions", ["archived_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column(
            "tool_args",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        # 可空：非 tool 行没有这个概念（NULL ≠ 成功）。
        sa.Column("tool_failed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # ★ 顺序的唯一依据 —— 让"两条消息同一个序号"从数据库层就不可能。
        sa.UniqueConstraint("session_id", "seq", name="chat_message_session_seq"),
        sa.CheckConstraint(
            f"role IN ({', '.join(repr(r) for r in _CHAT_ROLES)})",
            name="chat_message_role_valid",
        ),
    )
    # 读一个会话的全部消息：`WHERE session_id = ? ORDER BY seq`
    op.create_index("ix_chat_messages_session_seq", "chat_messages", ["session_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_seq", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_archived", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_owner_updated", table_name="chat_sessions")
    op.drop_table("chat_sessions")
