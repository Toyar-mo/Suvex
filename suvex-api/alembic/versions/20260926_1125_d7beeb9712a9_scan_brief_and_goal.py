"""scans 加任务背景（brief）与成功判据（goal）—— 任务 12

Revision ID: d7beeb9712a9
Revises: 99b1799eb3c6
Create Date: 2026-09-26 11:25:20.166484+00:00

## ⚠️ 自动生成的部分有**两处**不能直接用，都手工改了

### 一、`goal` 少了 `server_default` —— 在非空表上会直接失败

自动生成的是：

    op.add_column('scans', sa.Column('goal', sa.String(32), nullable=False))

在一个**已经有扫描记录**的库上，这句会报
`column "goal" of relation "scans" contains null values` —— 因为已有行
没有值可填，而列又不许为 NULL。

自动生成抓不到这一点：它只比对 schema，不知道表里有没有数据。
而"新加非空列"在**空库能过、有数据的库会炸**，正是迁移最危险的一类。

修法是先带 `server_default` 加上（已有行一次性填成默认值），
再把 default 摘掉 —— 保留 default 会让**将来忘记传 goal 的插入**
静默变成 `findVulns`，而那时我们更希望它报错（写入点只有一处，
显式传值比依赖数据库兜底清楚）。

### 二、`goal` 的 CHECK 约束要手工补

`--autogenerate` **不比对 CHECK 约束的内容**（本项目已经踩过一次：
`ck_findings_status_valid` 只允许 4 个状态，而代码已经写 6 个了）。
而 `models.py` 里那条 `goal_valid` 是真实存在的约束，
不补的话模型与库会分叉，下一次 autogenerate 又会想"该建"。

取值与 `core.vocab.ScanGoal` 一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = 'd7beeb9712a9'
down_revision: str | None = '99b1799eb3c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 `core.vocab.ScanGoal` 逐字一致。改那边就要改这里（反过来说：
# 这个字面量是"这一版迁移当时的取值集合"，本来就不该随后续版本漂移）。
_GOALS = ("findVulns", "captureFlag", "verifyFix")


def upgrade() -> None:
    # ① 任务背景。可空 —— "没写背景"是正常状态，不是缺数据。
    op.add_column('scans', sa.Column('brief', sa.Text(), nullable=True))

    # ② 成功判据。**先带 server_default 加**，理由见模块 docstring。
    op.add_column(
        'scans',
        sa.Column('goal', sa.String(length=32), nullable=False, server_default='findVulns'),
    )
    # 已有行填好了，摘掉 default —— 之后必须显式传值
    op.alter_column('scans', 'goal', server_default=None)

    # ③ 手工补 CHECK（autogenerate 不比对约束内容）
    op.create_check_constraint(
        'goal_valid',
        'scans',
        "goal IN (" + ", ".join(repr(v) for v in _GOALS) + ")",
    )


def downgrade() -> None:
    op.drop_constraint('goal_valid', 'scans', type_='check')
    op.drop_column('scans', 'goal')
    op.drop_column('scans', 'brief')
