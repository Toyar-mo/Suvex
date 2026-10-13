"""ctf2_solve_notes：解题方法卡（v0.32）

Revision ID: e7b2c9f41a83
Revises: d5e8f1a37c92
Create Date: 2026-10-12 18:30:00.000000+00:00

## 为什么加这张表

在这之前每次解题都从零开始：`resume` 只读**同一道题**的 transcript，
而 CTF2 的 `/writeups/` 端点只被用来提取 challenge_id 做同步 ——
解法内容本身没有被用于解题。于是"同类题第二次遇到"仍要重新摸索。

这张表存**方法**（题型、关键工具、做法梗概、走不通的方向），
供新题召回注入。

## ★ 它不存 flag —— 这是设计的硬约束

卡片会被拼进**另一道题**的 prompt。带上旧 flag 就变成"把 A 题的答案
泄漏到 B 题的上下文里"，而 B 题的模型完全可能把它当答案交上去。

所以写入路径逐字段脱敏（`notes.sanitize_for_reuse`），
而这张表**没有任何一列是给答案用的**。

## 与 `ctf2_solve_attempts` 的区别（两张表容易混）

| | `ctf2_solve_attempts` | `ctf2_solve_notes` |
| --- | --- | --- |
| 回答 | 这一次**按什么策略**跑的 | 这道题**怎么解** |
| 行数 | 每次受理一行（只增） | 每题一行（取最新） |
| 用途 | 审计与复盘 | 跨题复用 |

## 存量数据不回填

新表是空的。历史解题的 transcript 还在磁盘上，理论上可以离线提炼 ——
但那是一次独立的批处理，不该藏在迁移里（迁移要能秒级跑完）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "e7b2c9f41a83"
down_revision: str | None = "d5e8f1a37c92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ctf2_solve_notes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        # 题型（从 challenge 复制）。复制而不是引用：召回查询不必 join。
        sa.Column("category", sa.String(length=64), nullable=False, server_default=""),
        # 解出与否。失败的卡也有价值（`dead_ends`），只是排序靠后。
        sa.Column("solved", sa.Boolean(), nullable=False, server_default=sa.false()),
        # 三个正文列都**已脱敏**（见 notes.sanitize_for_reuse）。
        sa.Column("approach", sa.Text(), nullable=False, server_default=""),
        sa.Column("tools", sa.Text(), nullable=False, server_default=""),
        sa.Column("dead_ends", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name="fk_ctf2_solve_notes_challenge_id_ctf2_challenges",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ctf2_solve_notes"),
        # 一道题一张卡：重解时更新而不是新增。
        # 与 `ctf2_solve_attempts` 相反 —— 它们回答的问题不同。
        sa.UniqueConstraint("challenge_id", name="uq_ctf2_solve_notes_challenge"),
    )
    # 召回按 category 筛 + solved 排序，两列都要能走索引。
    op.create_index("ix_ctf2_solve_notes_category", "ctf2_solve_notes", ["category"])
    op.create_index("ix_ctf2_solve_notes_solved", "ctf2_solve_notes", ["solved"])


def downgrade() -> None:
    op.drop_index("ix_ctf2_solve_notes_solved", table_name="ctf2_solve_notes")
    op.drop_index("ix_ctf2_solve_notes_category", table_name="ctf2_solve_notes")
    op.drop_table("ctf2_solve_notes")
