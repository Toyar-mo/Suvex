"""新增 scan_hints 表 —— 运行中的人机协同（v0.57）

Revision ID: d7e4b2a9c1f3
Revises: c4d9e2f1a6b8
Create Date: 2026-10-22 15:00

## 这条迁移解决什么

P2 人机协同：智能体正在跑的时候，人看到它走错方向却没有办法拉回来 ——
只能等它跑完再重跑，浪费一轮 LLM 额度。这张表存「人在运行中补的线索」，
引擎每轮开始前取一条未读的、注入进下一轮上下文。

## 为什么是**新表**而不是在 scans 上加一列

线索是**多条**、有先后顺序、各自有「是否已送达」状态。加一列只能存
「最新一条」，先来那条还没被读到就被后来的覆盖 —— 等于人白打字。

## 列与约束

  · `id`        主键，`hint_<ULID>`（与项目其它 id 同风格）
  · `scan_id`   所属扫描，`index=True`（按扫描查是本表唯一查询模式）
  · `text`     线索正文，`Text` 不限长（长度上限在 service 层卡 500 字，
                DB 层不设 —— 上限是产品判断不是数据结构事实）
  · `author`   谁给的（留痕，不做外键：删成员也要解释得清当时是谁给的）
  · `consumed_at` 非空 = 已被某一轮注入过（幂等键，只注入一次）
  · `consumed_turn` 被注入进第几轮（诊断：解释「为什么我的线索像没生效」）
  · `created_at` 创建时刻

复合索引 `ix_scan_hints_scan_consumed (scan_id, consumed_at)` 服务于
「找该扫描下还没被读的线索」这一查询。

## 只增不删、不回填

新表无历史数据，`upgrade` 只建表，`downgrade` 只删表。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7e4b2a9c1f3"
down_revision: Union[str, None] = "c4d9e2f1a6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_hints",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("scan_id", sa.String(length=40), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_turn", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_scan_hints_scan_id", "scan_hints", ["scan_id"])
    op.create_index(
        "ix_scan_hints_scan_consumed", "scan_hints", ["scan_id", "consumed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_scan_hints_scan_consumed", table_name="scan_hints")
    op.drop_index("ix_scan_hints_scan_id", table_name="scan_hints")
    op.drop_table("scan_hints")
