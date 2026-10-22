"""沙箱 profile 的 CHECK 约束接纳 `ctf2-sandbox-ir`（v0.41）

Revision ID: c3d8f4b16a72
Revises: f2a9c81de604
Create Date: 2026-10-21 12:00

## 为什么需要这条迁移

`KernelSandboxProfile` 加了第三个取值 `ctf2-sandbox-ir`，三处模型定义都是
**从枚举生成**约束表达式的：

    CheckConstraint(
        f"sandbox_profile IN ({', '.join(repr(v) for v in values(KernelSandboxProfile))})",
        name="sandbox_profile_valid",
    )

于是模型侧自动就有了新取值 —— 但数据库里那条约束是**建库时用当时的字面量
固化下来的**，只有两个值。`alembic revision --autogenerate` **不比对 CHECK
约束的表达式**（它只比对表/列/索引），所以这个差异永远进不了自动生成的迁移。

症状不是"新镜像不生效"那么轻 —— 是一条**看起来完全无关**的报错：

    new row for relation "ctf2_challenges" violates check constraint
    "ck_ctf2_challenges_sandbox_profile_valid"
    DETAIL: Failing row contains (... ctf2-sandbox-ir ...)

实测于 2026-10-20 的 Kali 真跑：一条 IR 题建到一半，`sandbox_profile`
要写 `ctf2-sandbox-ir`，落库时被拒。而代码、提示词、镜像、路由全是对的
—— 唯一错的是数据库那一行字典。

## 为什么三条约束都要改

`KernelSandboxProfile` 被三张表用到，三处各有一条 CHECK：

| 表 | 约束名 | 列 |
| --- | --- | --- |
| `ctf2_challenges` | `ck_ctf2_challenges_sandbox_profile_valid` | `sandbox_profile` |
| `ctf2_environments` | `ck_ctf2_environments_sandbox_profile_valid` | `sandbox_profile` |
| `ctf2_config` | `ck_ctf2_config_default_sandbox_profile_valid` | `default_sandbox_profile` |

只改 `ctf2_challenges` 会留下更隐蔽的坑：单题能设成 IR 镜像，而
**配置里的默认镜像**仍然只能是那两个值 —— 也就是"IR 题能跑，
但没法让 IR 成为默认"，且报错一样是这句 CheckViolation，
排查的人会怀疑自己改了代码没重启。

## 为什么写死字面量而不是 import 枚举

与本项目其余迁移同一条纪律（见 `20261020_0330_e5c8a2d71b93` 的 docstring）：
CHECK 表达式一建好就固化在库里，而枚举**以后还会变**。迁移里 import 枚举，
今天生成的 SQL 会随枚举漂移 —— 回滚一条老迁移会造出一个"当时不存在"的
约束。迁移记录的是**那一刻**的事实。

## 顺带记的一条

这次是"枚举加了取值但忘了写迁移"的第 N 次。同一失败模式在
`TargetKind` 上被写进注释警告过（`app/core/vocab.py:63` 的 docstring：
"加取值时必须同时改三处……`--autogenerate` 不 diff 约束内容"）。
`KernelSandboxProfile` 也该有这么一句 —— 见本次同步加的注释。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8f4b16a72"
down_revision: str | None = "f2a9c81de604"
branch_labels: str | None = None
depends_on: str | None = None

# 收紧时用的旧集合（回滚要能回到这一版）
_OLD = ("ctf2-sandbox", "ctf2-sandbox-re")
# 放宽后的新集合（含 IR 镜像）
_NEW = ("ctf2-sandbox", "ctf2-sandbox-re", "ctf2-sandbox-ir")

# (表名, **未加前缀**的约束名, 列名)
#
# ⚠️ 这里必须写模型里那个裸名字（`name="sandbox_profile_valid"`），
#    不能写数据库里的全名（`ck_ctf2_challenges_sandbox_profile_valid`）：
#    `op.drop_constraint` / `op.create_check_constraint` 都会**再套一次**
#    项目的 naming_convention，传全名会拼成
#    `ck_ctf2_challenges_ck_ctf2_challenges_sandbox_profile_valid`。
#    实测报错：
#      ProgrammingError: constraint
#      "ck_ctf2_challenges_ck_ctf2_challenges_sandbox_profile_valid"
#      of relation "ctf2_challenges" does not exist
_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("ctf2_challenges", "sandbox_profile_valid", "sandbox_profile"),
    ("ctf2_environments", "sandbox_profile_valid", "sandbox_profile"),
    ("ctf2_config", "default_sandbox_profile_valid", "default_sandbox_profile"),
)


def _rewrite(values: tuple[str, ...]) -> None:
    """把三条约束重建成 `列 IN (<values>)`。

    ★ 必须 **先 drop 再 create**，不能指望 `create` 覆盖同名约束 ——
      PostgreSQL 里同名约束已存在时 `ADD CONSTRAINT` 直接报重名，
      而这里我们要换的是**表达式**，不是加一条新的。
    """
    literal = ", ".join(repr(v) for v in values)
    for table, name, column in _TARGETS:
        # 传裸名字，让 naming_convention 自己补 `ck_<table>_` 前缀。
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN ({literal})")


def upgrade() -> None:
    _rewrite(_NEW)


def downgrade() -> None:
    # ⚠️ 回滚前得先把现存数据里越界的值收回来，否则重建约束会失败：
    #    `ctf2-sandbox-ir` 不在旧集合里，ALTER TABLE 校验既有行时直接报错。
    #    收到 base 是唯一"安全方向"—— 它一定在新旧两个集合里，
    #    而且比"随便挑一个别值"诚实（base 是这三个里最保守的那个）。
    for table, _name, column in _TARGETS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = 'ctf2-sandbox' "
                f"WHERE {column} = 'ctf2-sandbox-ir'"
            )
        )
    _rewrite(_OLD)
