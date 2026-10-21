"""赛事 kind 接纳 `local`，且 `account_id` 改为可空（v0.43）

Revision ID: d5a9e3c72f81
Revises: a4f8d2c95b17
Create Date: 2026-10-21 14:00

## 这条迁移解决什么

应急响应复盘是**纯附件任务**：一批日志与证据包交给智能体还原入侵过程，
没有靶机、没有 flag、CTF2 平台上根本不存在这道题。它要复用 CTF2 的
解题流水线（ReAct 循环 / 三类事件 / 沙箱 / 三级证据分级 / 处置工单），
但**不该被迫先绑一个 CTF2 账号**。

于是新增 `kind == 'local'`：这一批题不属于任何平台账号。

两处 DDL：

1. `ck_ctf2_competitions_kind_valid` 从 `('competition','practice')`
   放宽到含 `'local'`；
2. `ctf2_competitions.account_id` 从 `NOT NULL` 改为 `NULL`。

## ★ 为什么必须手写，不能靠 `--autogenerate`

与上一条 `c3d8f4b16a72`（sandbox profile 加 IR 镜像）**同一条理由**：
`kind_valid` 在模型侧是从 `values(Ctf2CompetitionKind)` 生成的，所以
改枚举之后模型层自动就有了 `local`；而数据库里那条约束是**建库时用
当时的两个字面量固化下来的**。`--autogenerate` **不比对 CHECK 约束的
表达式**（只比对表 / 列 / 索引），这个差异永远进不了自动生成的迁移。

`account_id` 的 nullable 变化 autogenerate **会**看出来，但把它和
CHECK 的重建放在同一条迁移里是刻意的：两者是**同一个设计决定**的两半
（"本地案例不属于任何账号"），分开放会让"只应用了一半"变成一种可能，
而那一半的症状（能建 local 但必须挂假账号）恰好不会报错。

## 漏掉本迁移的症状

    IntegrityError: (psycopg.errors.CheckViolation)
    new row for relation "ctf2_competitions" violates check constraint
    "ck_ctf2_competitions_kind_valid"
    DETAIL: Failing row contains (... 'local' ...)

代码、接口、界面、worker 分支全对，唯一错的是数据库里那行字面量。
实测过一次同构的（2026-10-20 Kali 真跑，`sandbox_profile` 那版）。

## 为什么写死字面量而不是 import 枚举

与本项目其余迁移同一条纪律（见 `20261020_0330_e5c8a2d71b93` 与
`20261021_1200_c3d8f4b16a72` 的 docstring）：CHECK 表达式一建好就
固化在库里，而枚举**以后还会变**。迁移里 import 枚举，今天生成的 SQL
会随枚举漂移 —— 回滚一条老迁移会造出一个"当时不存在"的约束。
迁移记录的是**那一刻**的事实。

## 回滚要先把 local 行收回来

`account_id` 改回 NOT NULL 之前，必须处理已有的 `kind='local'` 行：
它们的 `account_id` 是 NULL，`ALTER COLUMN ... SET NOT NULL` 会直接
拒绝。这里的选择是**删掉本地复盘案例**而不是"随便挂一个账号" ——
本地案例的全部数据在附件目录与工单文件里，数据库这行只是个索引；
而给它挂一个假账号会让 `ondelete=CASCADE` 在解绑时把复盘记录连带
删掉，那是更糟的结局（删之前至少这次是用户要求的）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d5a9e3c72f81"
down_revision: str | None = "a4f8d2c95b17"
branch_labels: str | None = None
depends_on: str | None = None

# 收紧时用的旧集合（回滚要能回到这一版）
_OLD_KINDS = ("competition", "practice")
# 放宽后的新集合
_NEW_KINDS = ("competition", "practice", "local")

# ⚠️ 约束名写**裸名字**（模型里 `name="kind_valid"`），不能写数据库全名。
#    `op.drop_constraint` / `op.create_check_constraint` 会再套一次项目的
#    naming_convention，传全名会拼成
#    `ck_ctf2_competitions_ck_ctf2_competitions_kind_valid`。
#    这条坑在 `20261021_1200_c3d8f4b16a72` 里已经真踩过一次，实测报错：
#      ProgrammingError: constraint
#      "ck_ctf2_challenges_ck_ctf2_challenges_sandbox_profile_valid"
#      of relation "ctf2_challenges" does not exist
_KIND_CONSTRAINT = "kind_valid"
_KIND_TABLE = "ctf2_competitions"


def _rewrite_kind_constraint(kinds: tuple[str, ...]) -> None:
    """把 `kind_valid` 重建成 `kind IN (<kinds>)`。

    ★ 必须先 drop 再 create：PostgreSQL 里同名约束已存在时
      `ADD CONSTRAINT` 直接报重名，而这里要换的是**表达式**，
      不是加一条新的。
    """
    literal = ", ".join(repr(k) for k in kinds)
    op.drop_constraint(_KIND_CONSTRAINT, _KIND_TABLE, type_="check")
    op.create_check_constraint(_KIND_CONSTRAINT, _KIND_TABLE, f"kind IN ({literal})")


def upgrade() -> None:
    # ── ① account_id 先放开（必须在放宽 kind 之前）──────────────
    #
    # 顺序是有理由的：先放宽 kind、后改 nullable 的话，中间那一瞬间
    # 存在一个"允许写 local 但 account_id 还不接受 NULL"的窗口。
    # 单人部署下这个窗口没有实际影响，但迁移应该在任何应用顺序下都不
    # 制造一个自相矛盾的状态 —— 那是这类 bug 的典型来源。
    op.alter_column(
        _KIND_TABLE,
        "account_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    # ── ② 再放宽 kind ─────────────────────────────────────────
    _rewrite_kind_constraint(_NEW_KINDS)


def downgrade() -> None:
    # ⚠️ 顺序与 upgrade **相反**：先把 kind 收回来，再要求 account_id 非空。
    #    反过来的话，`SET NOT NULL` 会先因为 local 行的 NULL 而失败，
    #    报错指向 account_id，而真正的问题是还有 local 行。
    _rewrite_kind_constraint(_OLD_KINDS)
    # 本地复盘案例在旧模型里无法表达（它必须挂账号），所以只能删。
    # 删之前先把它们身上挂的题一起清掉 —— `ctf2_challenges.competition_id`
    # 是 `ondelete=CASCADE`，理论上删赛事行会自动带走题目，但显式删一次
    # 能让"删了多少"这件事可核对，而不是全靠外键的副作用。
    op.execute(
        sa.text(
            "DELETE FROM ctf2_challenges WHERE competition_id IN "
            "(SELECT id FROM ctf2_competitions WHERE kind = 'local')"
        )
    )
    op.execute(sa.text("DELETE FROM ctf2_competitions WHERE kind = 'local'"))
    op.alter_column(
        _KIND_TABLE,
        "account_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
