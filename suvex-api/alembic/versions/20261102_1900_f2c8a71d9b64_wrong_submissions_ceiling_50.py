"""kernel: 错误提交次数上限 10 → 50（v1.46，羊城杯赛制）

## 为什么改

赛事《环境规范说明》原文：每题 flag **最多提交 50 次**。而平台把自动
提交的错误次数上限（`max_wrong_submissions`）天花板压死在 10 —— 那是
早期"防止用户自己拆掉安全边界"的保守值。比赛当天要能配到 50，
否则这道题的可提交次数被我们本地闸门砍掉五分之四。

## 为什么必须手写迁移

上限写在 `KernelConfig.__table_args__` 的 `CheckConstraint`
（`max_wrong_submissions >= 0 AND max_wrong_submissions <= 10`）里，而
alembic 的 `--autogenerate` **不比对 CHECK 表达式**（本项目在
`KernelSandboxProfile` / `chat_turn_runs.status` 上各踩过一次）。
不手写这条，把上限调成 50 后应用层通过、数据库却拒绝 —— 症状是
"设置页显示已保存，其实没存进去"。

⚠️ 三处必须一起改，这条迁移是那个提醒：
  1. `settings/models.py` 的 `CheckConstraint` 字面量 → 50
  2. `kernel/service.py` 的 `MAX_WRONG_SUBMISSIONS_CEILING` → 50
  3. 本迁移（把已存在的库约束换成 50）

Revision ID: f2c8a71d9b64
Revises: b8d3f52a67e1
"""

from __future__ import annotations

from alembic import op

revision = "f2c8a71d9b64"
down_revision = "b8d3f52a67e1"
branch_labels = None
depends_on = None

_TABLE = "suvex_config"
_NAME = "max_wrong_submissions_within_ceiling"
_OLD_EXPR = "max_wrong_submissions >= 0 AND max_wrong_submissions <= 10"
_NEW_EXPR = "max_wrong_submissions >= 0 AND max_wrong_submissions <= 50"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _NEW_EXPR)


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _OLD_EXPR)
