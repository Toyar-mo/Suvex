"""ctf2_solve_usages + ctf2_writeups：token 消耗与题解（v0.36）

Revision ID: a1c7e4b92f68
Revises: f3a8d1c6b204
Create Date: 2026-10-14 22:00:00.000000+00:00

## 这次加两张表，对应两个需求

1. **每道题都要产出题解（Writeup）** —— 赛后提交的依据；
2. **token 消耗统计**，细分到每一次任务的消耗。

## ★ 表一：`ctf2_solve_usages`（每次 LLM 调用一行）

为什么不是"在 `ctf2_solve_attempts` 上加几个累计列"：

需求要的是"细分到**每一次任务**的消耗"，而那个说法有两种读法
（每次解题尝试 / 每次 LLM 调用）。独立表**同时满足两者** ——
按 `attempt_id` 聚合即得前者；反过来只存累计列，**明细永久丢失**。

明细能回答而累计值答不了的问题：
  · 哪一轮最贵（是不是某一轮烧掉了一半预算）；
  · 是不是在兜圈子烧钱（连续多轮高 token 但没进展）；
  · 换模前后各花多少（v0.35 的 fallback 让同一次尝试用两个模型）。

## ★ 表二：`ctf2_writeups`（一题一份）

`scans/writeup.py` 拼的是 **strix 报告**的重排版，而 CTF2 解题
**根本不走 strix** —— 它由 `app/kernel/runner.py` 自己的循环驱动，
从不产生 `penetration_test_report.md`。

所以 CTF2 的题借扫描的题解端点**永远**拿不到东西，而它报的
"产物目录可能被手工清理过"在误导用户去找一个不该存在的文件。

## ★★ 两张表的两条硬约束（改代码前先读）

### ① `usage_reported` 不能省

接入点可能不给 `usage`，那时 token 记 0 —— 而"真的是 0"与
"没记录到"在界面上长得一样。单开一列布尔区分，与 `providers` 的
`hasKey`/`apiKey` 分两列是同一条纪律。

### ② 题解里的 flag **不脱敏** —— 与 `ctf2_solve_notes` 恰好相反

| | `ctf2_solve_notes` | `ctf2_writeups` |
| --- | --- | --- |
| 去哪 | **另一道题**的 prompt | 赛后提交 / 给人看 |
| 带 flag | **泄漏** | 正确（flag 正是要展示的） |
| 所以 | 白名单式脱敏 | **原样保留** |

判据是**这份内容的去向**，不是"flag 是不是秘密"。
后来者若"顺手统一"两边，会把其中一边改坏。

## 存量数据不回填

新表是空的。历史解题的 usage 没有记录（当时没读），**不伪造** ——
统计接口会带 `since` 说明"从哪一刻起有数据"。
题解则可以在下一次解题时自然产出，或离线补跑。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "a1c7e4b92f68"
down_revision: str | None = "f3a8d1c6b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 表一：每次 LLM 调用的 token 用量 ──────────────────────
    op.create_table(
        "ctf2_solve_usages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        # 冗余存题 id：题解与总览都要"按题聚合"，只走 attempt 要多一次 JOIN。
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        # 存 model 而不是只存 provider_id：换模会让同一次尝试用两个模型。
        sa.Column("model", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("provider_label", sa.String(length=120), nullable=False, server_default=""),
        # 三个 token 列取自接入点的 prompt/completion/total ——
        # ⚠️ **不是** input_tokens/output_tokens（实测后者恒为 0）。
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        # ★ 接入点到底给没给 usage。False 时上面四个 0 不代表"没消耗"。
        sa.Column("usage_reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["ctf2_solve_attempts.id"],
            name="fk_ctf2_solve_usages_attempt_id_ctf2_solve_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name="fk_ctf2_solve_usages_challenge_id_ctf2_challenges",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ctf2_solve_usages"),
        # 0 轮意味着"一次不存在的调用"。
        sa.CheckConstraint("turn >= 1", name="solve_usage_turn_positive"),
        # 负数只可能是解析错误或上游脏数据 —— 宁可写库失败，也别污染统计。
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="solve_usage_tokens_non_negative",
        ),
    )
    # "这次尝试的逐轮明细"是最热查询（详情页展开）。
    op.create_index(
        "ix_ctf2_solve_usages_attempt_turn",
        "ctf2_solve_usages",
        ["attempt_id", "turn"],
    )
    # 按题聚合（题解里要写"这道题一共花了多少"）。
    op.create_index(
        "ix_ctf2_solve_usages_challenge_at",
        "ctf2_solve_usages",
        ["challenge_id", "created_at"],
    )

    # ── 表二：题解（一题一份）────────────────────────────────
    op.create_table(
        "ctf2_writeups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        # ★ 全文含 flag 原文（与 solve_notes 相反，见模块 docstring）。
        sa.Column("markdown", sa.Text(), nullable=False, server_default=""),
        # 未解出的题解同样保留 —— "试过什么、卡在哪"是可提交材料。
        sa.Column("solved", sa.Boolean(), nullable=False, server_default=sa.false()),
        # 冗余存轮数与耗时：题解是独立成品，该自带这些数字。
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_sec", sa.Integer(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name="fk_ctf2_writeups_challenge_id_ctf2_challenges",
            ondelete="CASCADE",
        ),
        # ⚠️ SET NULL 而不是 CASCADE：尝试记录被清掉时，题解仍然有价值
        #    （它是给人看的成品），不该跟着消失。
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["ctf2_solve_attempts.id"],
            name="fk_ctf2_writeups_attempt_id_ctf2_solve_attempts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ctf2_writeups"),
        # 一道题一份：重解时更新而不是新增（提交哪一份不该成为新问题）。
        sa.UniqueConstraint("challenge_id", name="uq_ctf2_writeups_challenge"),
    )
    op.create_index("ix_ctf2_writeups_generated_at", "ctf2_writeups", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_ctf2_writeups_generated_at", table_name="ctf2_writeups")
    op.drop_table("ctf2_writeups")

    op.drop_index("ix_ctf2_solve_usages_challenge_at", table_name="ctf2_solve_usages")
    op.drop_index("ix_ctf2_solve_usages_attempt_turn", table_name="ctf2_solve_usages")
    op.drop_table("ctf2_solve_usages")
