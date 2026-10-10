"""ctf2_config：解题并发配额（v0.22）

Revision ID: c8a1f4e27b93
Revises: b2d9f4a17c58
Create Date: 2026-10-01 08:00:00.000000+00:00

## 这一列修的是一处**真实存在的超卖**

原来 CTF2 的调度器直接读扫描那个上限
（`execution_settings.max_concurrent_scans`），而两个调度器是**各自**
数自己的名额：

    scans/scheduler.py   COUNT(Scan WHERE status='running')  <= 3
    ctf2/scheduler.py     len(self._running)（内存线程数）      <= 3

`ctf2/scheduler.py` 的模块 docstring 写着"复用同一个值是让『上限』
保持字面的意思（总的不超过 N）"，**而实现从来不是那样**。
`=3` 时真的能同时跑 3 个扫描 + 3 道题 = 6 个 —— 正是那份注释说要
避免的超卖，只是它发生在两个调度器**之间**，没有任何一处能看见。

决赛的两个模式（融合靶场安全评估 + AI Agent 解题）会在**同一场**
同时进行，这条超卖会实打实地把单机打爆。

## 修法是分层配额，不是加一个"总配额"旋钮

**总并发 = 各领域子配额之和**：

    max_concurrent_scans（扫描，已有）  +  max_concurrent_solves（解题，本列）

各领域守自己那一份就够了 —— 没有任何一边能越过自己的配额，
而总和天然有界。再加一个"总配额"旋钮会让三个数字互相约束，
配出互相矛盾的组合时还得决定谁赢，而那是个没有正确答案的规则。

## 存量行怎么处理

`server_default='3'` 而不是让列可空：

* 这一列**不是**"用户设过没有"的那种可空列（不像 `max_concurrent_scans`
  旁边的 `scan_max_turns`，那个的 NULL 有确切语义"跟着 env 走"）；
* 它没有 env 来源 —— 默认值来自代码常量，所以 NULL 只会多出
  "这一行还没设过"这一种要处理的状态，而它没有任何额外含义。

3 这个数与 `app/settings/models.py` 里 `Ctf2Config.max_concurrent_solves`
的 Python 默认值**是同一个数**，改任一处必须同时改另一处 ——
与 `max_wrong_submissions <= 10` 那条约束留在表上的提醒同一条理由。
（这里不用 CHECK 约束框住区间：它不是安全边界，而是一个可以按机器
规模调的业务配额。区间校验在 `settings.service` 里，那里能给出
"应在 1-16 之间"这种能读懂的报错。）

## 降级

`drop_column` 会把这一列整个丢掉，于是解题并发**静默回到借用扫描上限**
的老行为（也就是超卖回来）。执行降级的人要知道这一点。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "c8a1f4e27b93"
down_revision: str | None = "b2d9f4a17c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column(
            "max_concurrent_solves",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )


def downgrade() -> None:
    op.drop_column("ctf2_config", "max_concurrent_solves")
