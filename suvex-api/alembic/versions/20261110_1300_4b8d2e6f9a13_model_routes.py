"""`model_routes` —— 按扫描判据路由到不同 provider（v1.81）。

## 为什么需要

`scans.provider_id` 目前是用户每次手选的。对"渗透评估用强模型、
CTF 用便宜模型"这类策略，每建一次扫描都要重选一遍 —— 而选忘了的
症状不是报错，是**用错模型跑完一整轮**，事后只能从成本里看出来。

## 为什么不放进 `suvex_config`

那张表是 CTF 解题策略（提交/并发/沙箱），与"扫描用哪个模型"不是
同一类配置；混在一起会让"改提交策略"和"改模型路由"共享一条单例行，
一次误写就同时改了两件事。

Revision ID: 4b8d2e6f9a13
Revises: 9c1e4b7a2d63
Create Date: 2026-11-10 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "4b8d2e6f9a13"
down_revision = "9c1e4b7a2d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可移植 JSONB（与 `app/core/columns.py` 的 JsonColumn 同形）。
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("routes", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("model_routes")
