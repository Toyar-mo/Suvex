"""ctf2_solve_telemetry：解题的横向指标（v0.45）

Revision ID: c4e9a17f5b02
Revises: b7d2c4e91a35
Create Date: 2026-10-18 21:00:00.000000+00:00

## 这次加一张表，对应一个具体的问题

"跨题方法卡到底有没有用"——这个问题此前只能用**人工造的基准题**回答，
而实测那样做**测不出效应**：九道 mock 题干净基线平均 4.3 回合 / 5.3 次
工具调用，本来就没有重复探索可省（见 README「跨题方法卡」那一节，
n=1 时看到的"工具调用 −21%"在 n=5 下被推翻，差异 100% 来自一道
结构上两臂恒为 0 张卡的题）。

结论不是"方法卡没用"，是**这套测试测不出**。要判断它，得看真实比赛里的
真实题目 —— 而那里"这次跑了几轮、跑了几条命令、参考到几张卡"这三个数
本来就会自然产生，只是**从来没有人把它们记下来**。

## ★ 为什么是三个数（少一个都不行）

| 指标 | 记它在回答什么 | 少它的后果 |
| --- | --- | --- |
| `turns` | 对话轮了几次 | 题解表已有，但只覆盖"最后那一次" |
| `tool_calls` | **真正执行**了几条命令 | 回合数只反映一半：一回合可下发多条 |
| `notes_count` | 注入了几张同类题卡 | 无法区分"没卡"与"有卡"，分母永远不明 |

`turns` 与 `notes_count` 的关系是**分母与分子**的关系：
只有知道"这次本来有几张卡可参考"，"这次跑了几轮"才可比较。

## ★ 为什么独立成表，而不往 `ctf2_writeups` 加列

行数。`ctf2_writeups` 是 `UNIQUE(challenge_id)`（一题一份，重解时更新），
而统计要的是**样本**：同一道题解 5 次，题解表里永远只有 1 行，另外
4 次尝试的观测被覆盖掉 —— 而那 4 次恰恰是"后来者为什么更快"的证据。
写进题解表等于把样本数永久压成 1。

## ★ 外键口径：`challenge_id` CASCADE、`attempt_id` SET NULL

- 题被删掉 → 它的观测跟着没意义 → `CASCADE`；
- attempt 快照被清理 → 观测**仍然算数**（它说的是"这题解了几次、
  各花多少轮"）→ `SET NULL`。

与 `ctf2_writeups.attempt_id` 的取舍同源，结论也一致。

## ★ `category` 为什么冗余存

方法卡是**同分类**召回的，统计必须先按分类切开 —— 否则
"Web 题不需要卡"和"Crypto 题有卡也没用"会搅成一团。而
`challenges.category` 会在同步时被平台侧改动，一旦改了，历史观测的
分类就跟着漂。冗余一份，让"当时的分类"与"当时的张数"来自同一快照。

## 不存 `elapsed_sec`

它的来源是**借壳的 `Scan.duration_sec`**（`Ctf2Challenge` 自己没有
耗时列），而那值要等 `finalize_solve_scan` 才存在；本表的写入点在
`_record_outcome`，那时它还是 `None`。耗时在 `ctf2_writeups` 里已有，
且实测**没有分辨力**（宿主负载能把同一题放大三倍），不为它在这里
再造一处会互相打架的读数。

## 存量数据不回填

新表是空的，**不伪造**。历史解题当时没记这两个数，现在也补不出来 ——
凭空算一个出来会让"从哪一刻起有数据"变得不可知。统计接口据此要能
说明起始时间（与 `a1c7e4b92f68` 里 token 统计的处理一致）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "c4e9a17f5b02"
down_revision: str | None = "b7d2c4e91a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ctf2_solve_telemetry",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        # ⚠️ SET NULL：快照被清掉后这条观测仍然算数（见模块 docstring）。
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        # 当时的分类（防止同步改分类让历史统计漂移）。
        sa.Column("category", sa.String(length=32), nullable=False, server_default=""),
        # 所有统计都要能按"解出/没解出"分组。
        sa.Column("solved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        # 真正执行过的工具调用次数（不含被 MAX_ACTIONS_PER_TURN 丢掉的动作）。
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        # 注入的同类题方法卡张数。0 = 没召回（分类为空/库里没有/召回失败）。
        sa.Column("notes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name="fk_ctf2_solve_telemetry_challenge_id_ctf2_challenges",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["ctf2_solve_attempts.id"],
            name="fk_ctf2_solve_telemetry_attempt_id_ctf2_solve_attempts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ctf2_solve_telemetry"),
        # 负数只可能是记账错误 —— 宁可写库失败，也别污染统计。
        sa.CheckConstraint(
            "turns >= 0 AND tool_calls >= 0 AND notes_count >= 0",
            name="solve_telemetry_non_negative",
        ),
    )
    # "这道题的历次观测"（详情页/复盘按题看）。
    op.create_index(
        "ix_ctf2_solve_telemetry_challenge_at",
        "ctf2_solve_telemetry",
        ["challenge_id", "created_at"],
    )
    # "按分类算均值"（回答"方法卡在哪类题上才有用"）。
    op.create_index(
        "ix_ctf2_solve_telemetry_category_at",
        "ctf2_solve_telemetry",
        ["category", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ctf2_solve_telemetry_category_at", table_name="ctf2_solve_telemetry"
    )
    op.drop_index(
        "ix_ctf2_solve_telemetry_challenge_at", table_name="ctf2_solve_telemetry"
    )
    op.drop_table("ctf2_solve_telemetry")
