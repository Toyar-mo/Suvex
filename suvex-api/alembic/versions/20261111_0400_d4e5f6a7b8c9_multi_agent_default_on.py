"""多智能体默认开启（v1.89）。

## 为什么需要一条数据迁移

代码里的默认值改了（`KernelConfig.enable_multi_agent=True`），但**已有部署
的单例行里存着 False** —— 任何一次在设置页保存都会把当时的默认值落库，
而那次保存多半只是改了个超时。只改代码默认值的话，线上永远看不到变化，
症状是"改了却没生效"。

所以这里把已有单例行显式翻成 true。想恢复旧的单循环行为：设置页关掉，
或 `alembic downgrade -1`。

⚠️ 降级会把值写回 false，即使之后有人手动打开过 —— 它只适合"确认要回滚"。
"""

from __future__ import annotations

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE suvex_config SET enable_multi_agent = true WHERE id = 'default'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE suvex_config SET enable_multi_agent = false WHERE id = 'default'"
    )
