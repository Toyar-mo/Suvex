"""ctf2_config.fallback_provider_id：换模 fallback（v0.35）

Revision ID: f3a8d1c6b204
Revises: e7b2c9f41a83
Create Date: 2026-10-14 04:20:00.000000+00:00

## 为什么加这一列

解题是一个串行循环，每轮调一次 LLM。默认 provider 一旦返回不可重试的
错误（401 / 429 / 5xx / 长时间过载），当前行为是**整道题判失败** ——
而那时可能已经跑了三十多轮、在沙箱里做完了大量分析。实测一次
`ReadError` 让跑到第 14 轮、已反汇编出完整 IL 的题全部作废。

这一列存"失败时改用哪个 provider"，让那几十轮不至于白费。

## ★ 可空 = 默认关闭

NULL（默认）表示**不换模**，行为与迁移前完全一致 —— 不替用户编一个
他没设过的行为。配了才生效。

## 为什么没有外键

与 `ctf2_solve_attempts.provider_id` 同一条理由：删掉一个 provider
不该被这里拦住。取不到那个 id 时按"没配"处理（见 runner 的换模逻辑）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "f3a8d1c6b204"
down_revision: str | None = "e7b2c9f41a83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column("fallback_provider_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ctf2_config", "fallback_provider_id")
