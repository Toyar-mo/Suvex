"""ctf2_solve_attempts：解题策略快照（v0.28）

Revision ID: d5e8f1a37c92
Revises: c1f5a83d2e07
Create Date: 2026-10-11 23:30:00.000000+00:00

## 为什么加这张表

在这之前，`POST /ctf2/challenges/{id}/solve` 的三个受理期输入
**全部没有真正生效**：

| 输入 | 实际发生了什么 |
| --- | --- |
| `mode` | `service.start_solve` 收下后直接 `del mode`，worker 现读**当时的全局设置** |
| `brief` | schema 收了、router 没转交、service 没字段、prompt 里没有位置 |
| `max_turns` | 与 `mode` 在 `build_spec` 里共用同一个 `settings_row`，同样现读 |

`mode` 那条最严重，它是一条**时序漏洞**而不是"参数没生效"：

  用户点解题时选 `safe`（明确表示"我要人工确认"）→ 题目排队等待
  （并发上限 3）→ 这期间任何人把全局改成 `auto` → worker 启动时读到
  的就是 `auto` → **系统替用户做了一个他明确拒绝过的对外提交**，
  而消耗的是**不可再生**的错误次数额度。

## 为什么新建表而不是给 challenge 加列

这些值是**这一次尝试**的输入，不是题目的属性：同一道题第二次解可能
换 `mode`（上次 safe 没确认，这次想 auto）、换 `brief`（补了新线索）、
换模型（上次那个没解出来）。存到 challenge 上会让"上一次用什么策略"
被下一次覆盖 —— 而事后复盘"它当时是按什么策略跑的"正是最需要能回答的。

与 `ctf2_environments` 存"同一道题起过三次环境"是同一条理由。

## ⚠️ 存量数据不回填

表是空的（新建），没有存量行需要处理。历史解题记录**不会**凭空
长出策略快照 —— 那是对的：我们不知道它们当时用的是什么，编一个
填进去等于伪造证据。`build_spec` 对"没有快照"的任务保留读全局
设置的旧行为（兜底），所以升级后已在队列里的任务仍能跑完。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "d5e8f1a37c92"
down_revision: str | None = "c1f5a83d2e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ctf2_solve_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        # ── 受理时定下的策略（worker 只读这里）──────────────
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("sandbox_profile", sa.String(length=32), nullable=False),
        # 用户补充的线索。可空：多数题没有额外提示。
        sa.Column("brief", sa.Text(), nullable=True),
        # 这次尝试用的是哪个模型。可空：受理时若还没配 provider，
        # `build_spec` 会抛错并把题目置 failed —— 那时这行仍然是
        # "受理过的证据"，不该因为没模型就不落库。
        sa.Column("provider_id", sa.String(length=64), nullable=True),
        sa.Column("llm_model", sa.String(length=160), nullable=True),
        sa.Column("resume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name="fk_ctf2_solve_attempts_challenge_id_ctf2_challenges",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ctf2_solve_attempts"),
        # ── 取值合法性 ────────────────────────────────────
        #
        # 与 `ctf2_submissions.mode_valid` 同一个理由：这两列会进审计
        # 与复盘，写进一个拼错的字符串会让"这次到底按什么策略跑的"
        # 变成读不出来的东西。
        #
        # ⚠️ 与 `vocab.Ctf2SubmitMode` / `Ctf2SandboxProfile` 是同一对：
        #    CHECK 表达式建好就固化在库里，而枚举能改 —— 两者不同步会
        #    表现成"应用层放行、数据库拒绝"。加新取值时两边一起改。
        sa.CheckConstraint(
            "mode IN ('safe', 'auto')", name="solve_attempt_mode_valid"
        ),
        sa.CheckConstraint(
            "sandbox_profile IN ('ctf2-sandbox', 'ctf2-sandbox-re')",
            name="solve_attempt_profile_valid",
        ),
        # 回合数必须有下界：0 或负数意味着"受理了但一轮都不跑"，
        # 那是配置错误而不是预算选择。
        sa.CheckConstraint("max_turns >= 1", name="solve_attempt_turns_positive"),
    )
    # "这道题最近几次尝试"是最常见的查询（界面要显示上次用了什么策略）。
    op.create_index(
        "ix_ctf2_solve_attempts_challenge_at",
        "ctf2_solve_attempts",
        ["challenge_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ctf2_solve_attempts_challenge_at", table_name="ctf2_solve_attempts")
    op.drop_table("ctf2_solve_attempts")
