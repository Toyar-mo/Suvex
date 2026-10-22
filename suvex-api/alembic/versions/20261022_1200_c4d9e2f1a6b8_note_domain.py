"""方法卡加 domain 列 —— 跨场景能力复用（v0.56）

Revision ID: c4d9e2f1a6b8
Revises: b8f3a1c7d2e4
Create Date: 2026-10-22 12:00

## 这条迁移解决什么

「通用智能体」的标志是：**在一个场景学到的方法，换个场景还能用**。
而方法卡（`suvex_solve_notes`）原来只按 `category` 精确召回 ——
渗透扫描拿不到应急响应的卡，反之亦然。

本迁移加一列可空的 `domain`（`offensive` / `defensive` / `generic`）：

  · 取值 `generic` 的卡会被**所有**分类召回（`notes_for_challenge`
    的 `WHERE` 变成 `category = ? OR domain = 'generic'`）；
  · 其余取值或 `NULL` 只按 `category` 精确召回 —— 与改动前**逐字节相同**。

## 为什么是可空列、为什么不需要回填

`NULL` = 加这列之前写的旧卡（历史数据）。它们的召回行为必须与
改动前完全一致，所以：

  · 加列用 `nullable=True`、无 server_default —— 旧行天然是 `NULL`，
    行为不变；
  · **不回填**：给旧卡批量猜一个 domain 会让它们改变召回面（可能从
    「只在 web 分类可见」变成「全分类可见」），那是行为变更不是迁移；
  · **不加 CHECK**：取值靠写入侧的 `vocab.normalize_domain` 收敛。加 CHECK
    会在将来加第四档（如 `forensic`）时要求再写一条迁移改约束，而这一列
    本质是软标签，不值得上硬约束。

## 需要索引吗

不需要单独建索引。召回查询是
`WHERE (category = ? OR domain = 'generic') AND challenge_id <> ?`，
`category` 已有索引（`ix_suvex_solve_notes_category`），`domain='generic'`
的选择性很低（多数卡是 `NULL`/`offensive`/`defensive`），全表扫这一小表
（方法卡数量级：几百张）代价可忽略。为它建索引是过度优化。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e2f1a6b8"
down_revision: Union[str, None] = "b8f3a1c7d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "suvex_solve_notes",
        sa.Column("domain", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("suvex_solve_notes", "domain")
