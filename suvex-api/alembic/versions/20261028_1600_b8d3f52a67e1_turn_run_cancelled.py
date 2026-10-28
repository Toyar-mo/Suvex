"""chat: `chat_turn_runs.status` 允许 `cancelled`（v1.25）

## 为什么需要这个状态

用户按「彻底停止」时，那一轮不是失败、也不是"跑完了"：
- 落 `done` 会被当成正常结束（界面/统计都看不出是用户停的）；
- 落 `interrupted` 会被后台 `resumer` 自动接手 —— 与"别再跑了"完全相反；
- 落 `failed` 会被标成红色错误，并可能弹「重试」按钮。

所以新增 `cancelled`：终态、不接手、不报错。

## 为什么必须手写迁移

`TURN_RUN_STATUSES` 生成的是 `CheckConstraint`，而 alembic 的 `--autogenerate`
**不比对 CHECK 表达式**（本项目在 `KernelSandboxProfile` 上踩过同一个坑）。
不手写这条，新状态在 PostgreSQL 上会直接被约束拒绝。

Revision ID: b8d3f52a67e1
Revises: a3c7e91d4f28
"""

from __future__ import annotations

from alembic import op

revision = "b8d3f52a67e1"
down_revision = "a3c7e91d4f28"
branch_labels = None
depends_on = None

_TABLE = "chat_turn_runs"
_NAME = "chat_turn_run_status_valid"
_OLD = ("running", "done", "failed", "interrupted")
_NEW = ("running", "done", "failed", "interrupted", "cancelled")


def _expr(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(repr(v) for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _expr(_NEW))


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _expr(_OLD))
