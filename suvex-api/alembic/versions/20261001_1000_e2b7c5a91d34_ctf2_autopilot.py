"""ctf2_config：自动增量同步、解题队列轮询、新题自动优先（v0.24）

Revision ID: e2b7c5a91d34
Revises: d4e7b19c3f28
Create Date: 2026-10-01 10:00:00.000000+00:00

## 这次加的四列，都在回答同一个问题：**发现得太晚**

比赛规则：

> 每道题目前三名成功解题的团队将额外获得本题分值的 15%、10% 和 5%
> 的奖励分

"前三名"这三个字把得分从"解得多快"改成了"**多早开始**"。而在这次
迁移之前，一道题在平台上挂出来之后的处境是：

    平台上有了 → （系统里不存在）→ 用户想起来点同步 → 才入队

第一段箭头里的人工延迟是**纯粹被让掉的分**，而且它跟解题能力无关 ——
对手的 Agent 如果每分钟同步一次，他在这道题上就已经领先一个身位。

## 四列各自的值与理由

| 列 | 默认 | 为什么是这个值 |
|---|---|---|
| `solve_poll_seconds` | 1.0 | 认领延迟直接换分；一个 tick 是一次带行锁的单表 SELECT |
| `auto_sync_seconds` | 30 | 增量同步仅 1 次 writeups + 每个新题 1 次探测；30 秒 = 2 req/min |
| `auto_priority_new` | true | 新题排在旧题前，新题内部按分值降序 |
| `auto_start_new` | false | 见下：它单独默认关 |

## ★ 为什么"自动开解"默认关，而另外两个默认开

判据是**可逆性**，不是"哪个更有用"：

* 自动同步只**读**平台，零副作用；而"忘了开"的代价是确定的（丢加分）
* 自动提优先级只换一个排队位置，用户在题目列表上看得见、能改回来
* 自动开解会**真的消耗**模型额度和解题并发名额。在一场"题量明显超过
  人工手工完成能力"的比赛里，它可能把 3 个名额全占在几道根本解不出的
  题上 —— 那是一个**不确定**的代价

默认值应该站在"错不起"的那一侧。

## ⚠️ 关于 `solve_poll_seconds` 与 `scan_poll_seconds` 的分离

这一列存在的**唯一**理由是切断一处耦合：在此之前 `ctf2/scheduler.py`
读的是 `settings.scan_poll_seconds`（扫描的旋钮）。用户在设置页把扫描
轮询从 2 秒调到 10 秒（一个完全合理的省机器操作），CTF2 的认领延迟会
**一起**变成 10 秒，而界面上没有任何地方提示这两件事连着。

这与 v0.22 的 `max_concurrent_solves` 是同一类病（**两个独立的东西共用
一个旋钮**，那次表现为超卖）。存量行给 `server_default='1.0'`：升级后
CTF2 从"跟随扫描的 2 秒"变成"自己的 1 秒"，那是**变快**，没有行为回退
的风险。

## 两条 CHECK 与 vocab 里的常量是同一对

`vocab.MIN/MAX_CTF2_SOLVE_POLL_SECONDS` 与这里的字面量必须一起改。
理由与 `max_wrong_submissions_within_ceiling` 完全一致：CHECK 表达式
建好就固化在库里，而常量能改 —— 写常量会让"改了常量但没写迁移"表现
为"应用层放行、数据库拒绝"。

⚠️ `solve_poll_seconds` 的下界 0.5 **不是建议值而是护栏**：填 0 会让
调度线程变成忙等，在比赛现场表现为 CPU 打满、解题反而更慢。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "e2b7c5a91d34"
down_revision: str | None = "d4e7b19c3f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column("solve_poll_seconds", sa.Float(), nullable=False, server_default="1.0"),
    )
    op.add_column(
        "ctf2_config",
        sa.Column("auto_sync_seconds", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "ctf2_config",
        sa.Column(
            "auto_priority_new",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "ctf2_config",
        sa.Column(
            "auto_start_new",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "solve_poll_seconds_within_range",
        "ctf2_config",
        "solve_poll_seconds >= 0.5 AND solve_poll_seconds <= 30",
    )
    op.create_check_constraint(
        "auto_sync_seconds_within_range",
        "ctf2_config",
        "auto_sync_seconds >= 0 AND auto_sync_seconds <= 3600",
    )


def downgrade() -> None:
    op.drop_constraint("auto_sync_seconds_within_range", "ctf2_config", type_="check")
    op.drop_constraint("solve_poll_seconds_within_range", "ctf2_config", type_="check")
    op.drop_column("ctf2_config", "auto_start_new")
    op.drop_column("ctf2_config", "auto_priority_new")
    op.drop_column("ctf2_config", "auto_sync_seconds")
    op.drop_column("ctf2_config", "solve_poll_seconds")
