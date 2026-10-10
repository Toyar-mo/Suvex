"""ctf2_challenges：把候选 flag 与已确认 flag 拆成两列（v0.20）

Revision ID: b2d9f4a17c58
Revises: f7b3c2a91e04
Create Date: 2026-09-28 17:00:00.000000+00:00

## 为什么必须拆

拆分前 `ctf2_challenges.flag` 一个列装两种语义：

* 「LLM 提出的候选」—— `candidate_ready` 状态下出现在界面上；
* 「平台判过对的答案」—— `solved` 之后出现在界面上。

于是界面无法回答「**现在有没有东西等着我确认**」。真会出事的那条路径：

    safe 模式落候选 A（界面显示 A）→ 用户确认提交 → 平台判错 →
    状态退回未解出，而 A 还挂在题目行上

结果是一道「没解出却有一个 flag」的题，而那个字符串看起来完全像答案。

## 升级时的数据搬迁是**必须**的，不是可选的

`confirmed_only_when_solved` 这条 CHECK 一旦建上，
「有 flag 但状态不是 solved」的存量行就会让**整个升级失败**
（CHECK 会校验已有数据）。所以顺序是：

1. 重命名 `flag` → `confirmed_flag`；
2. 加 `candidate_flag` 列；
3. **搬迁**：非 solved 的行把值挪到 `candidate_flag`，`confirmed_flag` 置 NULL；
4. 最后才建两条 CHECK。

反过来做（先建 CHECK 再搬迁）会让这次升级在有候选数据的库上直接报错 ——
而那些数据恰恰是最常见的状态（一道题解到一半）。

## 降级会把候选合并回单列

`flag = COALESCE(flag, candidate_flag)`：优先保留已确认的那个。
这是**有损**的（拆分本身带来的信息会丢），但降级到旧 schema 之后
本来就没有地方能表达「这是候选」—— 记在这里，让执行降级的人知道，
而不是让它在某次回滚里无声发生。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "b2d9f4a17c58"
down_revision: str | None = "f7b3c2a91e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 重命名。已有的 `ck_ctf2_challenges_solved_implies_flag` 里引用的
    #    列名会被 PostgreSQL **自动跟着改**（CHECK 存的是解析后的表达式），
    #    所以这里不需要 drop/create 那条约束。
    op.alter_column(
        "ctf2_challenges",
        "flag",
        new_column_name="confirmed_flag",
        existing_type=sa.Text(),
        existing_nullable=True,
    )

    # ② 新列。
    op.add_column(
        "ctf2_challenges", sa.Column("candidate_flag", sa.Text(), nullable=True)
    )

    # ③ 搬迁存量数据（见模块 docstring 第 3 条：必须在建 CHECK 之前）。
    #    这个 UPDATE 同时满足了 `candidate_never_with_confirmed`
    #    （candidate 与 confirmed 不会同时有值）。
    op.execute(
        sa.text(
            "UPDATE ctf2_challenges "
            "   SET candidate_flag = confirmed_flag, confirmed_flag = NULL "
            " WHERE confirmed_flag IS NOT NULL AND status <> 'solved'"
        )
    )

    # ④ CHECK。⚠️ 手写（任务清单点名要求）：`--autogenerate` 对 CHECK
    #    只比对"有没有这条约束"，不比对里面的表达式 —— 本项目在
    #    `findings.status` 上真踩过一次（迁移 `b1f7c0a25e38`）。
    op.create_check_constraint(
        "candidate_never_with_confirmed",
        "ctf2_challenges",
        "candidate_flag IS NULL OR confirmed_flag IS NULL",
    )
    op.create_check_constraint(
        "confirmed_only_when_solved",
        "ctf2_challenges",
        "confirmed_flag IS NULL OR status = 'solved'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_ctf2_challenges_confirmed_only_when_solved"),
        "ctf2_challenges",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ctf2_challenges_candidate_never_with_confirmed"),
        "ctf2_challenges",
        type_="check",
    )

    # 合并回单列：优先保留已确认的那个（见模块 docstring）。
    op.execute(
        sa.text(
            "UPDATE ctf2_challenges "
            "   SET confirmed_flag = COALESCE(confirmed_flag, candidate_flag)"
        )
    )

    op.drop_column("ctf2_challenges", "candidate_flag")
    op.alter_column(
        "ctf2_challenges",
        "confirmed_flag",
        new_column_name="flag",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
