"""`suvex_config.wall_clock_seconds` —— CTF 每题墙钟预算（v1.61）。

## 为什么加这一列

CTF 求解路径此前**没有墙钟**（`SolveSpec.wall_clock_seconds` 恒为 None）。
后果见 `app/core/vocab.py::DEFAULT_CTF2_SECONDS_PER_TURN` 的说明：墙钟三档
催促（60/80/92%，唯一按时间轴的维度）一次都没触发过，只剩回合催兜底；
而回合催治不了"一轮花很久"的题 —— 真库实测有题跑满 80 轮、单次求解墙钟
烧到 9.5 小时，中间催促全程沉默。

## 为什么可空，且 NULL = "自动"（不是"关闭"）

与 `fallback_provider_id`（NULL = 关闭）语义**相反**：这里 NULL 表示
"按 `max_turns × 45` 自动推导"（见 `vocab.default_ctf2_wall_clock_seconds`）。
零配置就修好病态长跑，且自动值与回合预算联动，不会出现"改了回合忘了改
墙钟"的错位。显式填数则覆盖自动值（区间校验在 service 层，不在这里）。

⚠️ 不加 CHECK 约束：区间（120–86400）属于**应用层策略**，随
`MIN/MAX_CTF2_WALL_CLOCK_SECONDS` 变化；写进 DB 的 CHECK 会让每次改区间
都要再来一条迁移，而错位的症状是"调了上限却存不进去"。校验放在
`service._assert_wall_clock_seconds`（越界 → 400，不静默夹取）。

Revision ID: a7e3c9d24b61
Revises: f2c8a71d9b64
Create Date: 2026-11-03 22:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a7e3c9d24b61"
down_revision = "f2c8a71d9b64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suvex_config",
        sa.Column(
            "wall_clock_seconds",
            sa.Integer(),
            nullable=True,
            comment=(
                "CTF 每题墙钟预算（秒）；NULL = 按 max_turns×45 自动推导，"
                "非 NULL 覆盖（区间 120-86400，应用层校验）"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("suvex_config", "wall_clock_seconds")
