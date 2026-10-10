"""ctf2_challenges：调度优先级（v0.23）

Revision ID: d4e7b19c3f28
Revises: c8a1f4e27b93
Create Date: 2026-10-01 09:00:00.000000+00:00

## 为什么加这一列

比赛计分规则里有两条直接指向它：

> 每道题目前三名成功解题的团队将额外获得本题分值的 **15%、10%、5%**
> 的奖励分
> 参赛队伍可以持续与自行构建的 Agent 进行交互，包括设定任务目标、
> **调整解题优先级**、查看运行状态、优化提示词……

"前三名加分"意味着**速度直接换分**；而"调整解题优先级"是赛事方
**明确允许并期待**的操作。原来认领顺序是纯 FIFO（按 `updated_at`），
用户唯一能做的干预是"取消再重排" —— 既不精确，也会把那道题的状态
搅乱（取消会把 `solving` 打回，而重排后 `updated_at` 又变了）。

## 存量行全部 0，这是正确行为

`server_default='0'` 且不加 CHECK 区间：

* 排序是 `priority DESC, updated_at ASC`，所以**全 0 时行为与升级前
  完全一致**（纯 FIFO，按 `updated_at`）—— 没有人需要为这次升级
  改变任何操作习惯；
* 它是一个纯排序权重，任何整数都有意义（负数表示"往后排"也是合理的
  用法："这道题先别做"）。框一个区间只会让"我想让它排最前"变成
  "先查一下上限是多少"，而那个查询没有任何人会做。

## 不建议加的索引（如实记下代价）

认领查询带 `WHERE status='solving' ORDER BY priority DESC, updated_at`。
理想情况下该有一个 `(status, priority DESC, updated_at)` 的部分索引。

**但这里刻意不加**：`ctf2_challenges` 的总行数是**一场比赛几百道**这个
量级，而 `status='solving'` 会把候选集缩到个位数（并发上限只有几十）。
在这个规模上，顺序扫描一个已经用 `ix_ctf2_challenges_competition_status`
筛过的结果集，与走一个新索引没有可测量的差别 —— 而多一个索引意味着
每次状态变更都要多维护一棵 B 树。

⚠️ 如果将来题目量到了几万道，这一条是该重新评估的第一处。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "d4e7b19c3f28"
down_revision: str | None = "c8a1f4e27b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_challenges",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ctf2_challenges", "priority")
