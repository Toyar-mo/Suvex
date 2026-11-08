"""`chat_sessions.challenge_id` —— 一道题一个对话（CTF 页重做第 2 步）。

## 为什么加这一列

看板上的「查看」要回到"这道题的那个会话"。没有关联字段时，只能靠标题
去猜 —— 而标题会被用户改名，也可能两道题同名。

## 为什么不建外键

题目会随着"重新同步 / 换接入"被删掉，而那段对话是有价值的历史记录
（"我们当时怎么试的"），不该跟着级联消失。悬空 id 的表现是题目条上
写"题目已不存在"，那是看得见的，不会误导。

Revision ID: 9c1e4b7a2d63
Revises: a7e3c9d24b61
Create Date: 2026-11-08 19:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "9c1e4b7a2d63"
down_revision = "a7e3c9d24b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "challenge_id",
            sa.String(length=64),
            nullable=True,
            comment="关联的 CTF 题目 id；NULL = 普通对话（不建外键，见迁移说明）",
        ),
    )
    op.create_index(
        "ix_chat_sessions_owner_challenge",
        "chat_sessions",
        ["created_by", "challenge_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_owner_challenge", table_name="chat_sessions")
    op.drop_column("chat_sessions", "challenge_id")
