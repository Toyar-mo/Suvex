"""多智能体编排开关 —— KernelConfig 加 enable_multi_agent 列（v0.58）

Revision ID: e8c5f1a3d927
Revises: d7e4b2a9c1f3
Create Date: 2026-10-22 16:00

## 这条迁移解决什么

v0.58 给引擎加了完整的多智能体编排（root 调度 + 子 agent 并行执行）。
它是一个**行为开关**（打开后一次任务的 LLM 调用量按子 agent 数量放大约
N 倍），与两个场景包开关一样由人显式打开 —— 所以需要一个持久化列。

## 为什么单独一列而不是复用场景包开关

`enable_agentsec_scenarios` / `enable_ir_scenarios` 换的是**行为纪律**
（攻 vs 只读复盘）；多智能体换的是**执行拓扑**（单循环 vs 编排），与
分类无关 —— 任何题型都可以跑单循环或编排。三者是相互独立的三维，
共用一个开关会让"打开任意一个"顺带改变另外两个的语义。

## 为什么 server_default=text("false") 而不是只靠 ORM default

只写 ORM default 时，已存在的行在 ALTER 后是 **NULL**（PostgreSQL 不会
用 ORM 默认值回填存量行），而列声明是 `nullable=False` → 迁移当场失败
或有脏数据。所以迁移里必须显式 `server_default` 把存量行填成 `false`
（= 关闭 = 与引入这个功能之前行为一致），这也正是安全方向。

## 为什么保留 server_default

保留它意味着将来任何**绕过 ORM** 的插入（运维脚本、手工 SQL）也拿到
`false` 而不是报 NOT NULL 错。与 `enable_ir_scenarios` 那条迁移同一处理。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8c5f1a3d927"
down_revision: Union[str, None] = "d7e4b2a9c1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "suvex_config",
        sa.Column(
            "enable_multi_agent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("suvex_config", "enable_multi_agent")
