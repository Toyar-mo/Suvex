"""重建 `ctf2_solve_attempts` 的 sandbox_profile 取值约束

Revision ID: e7b2c48d1a93
Revises: d5a9e3c72f81
Create Date: 2026-10-21 15:00

## 这条迁移补的是什么

v0.41 加 `KernelSandboxProfile.IR`（`ctf2-sandbox-ir`）时，迁移
`c3d8f4b16a72` 重建了**三张**表上的取值约束：

| 表 | 约束 | 列 |
| --- | --- | --- |
| `ctf2_challenges` | `sandbox_profile_valid` | `sandbox_profile` |
| `ctf2_environments` | `sandbox_profile_valid` | `sandbox_profile` |
| `ctf2_config` | `default_sandbox_profile_valid` | `default_sandbox_profile` |

**漏了第四张**：`ctf2_solve_attempts` 也有一列 `sandbox_profile`
（每次受理时的策略快照），它的约束 `solve_attempt_profile_valid` 同样
由 `values(KernelSandboxProfile)` 生成 —— 模型侧自动带上了新值，
数据库里那条却还是建库时固化的两个字面量。

## ★ 为什么漏了三个月都没被发现

前三条约束在**建题**、**起环境**、**改全局设置**时就会撞上，都是高频
路径。而这一条只在**真正受理一次解题**时触发 —— 更具体地说，只在
`start_solve()` 里 `KernelSolveAttempt(...)` 落库的那一刻。

于是"漏了这张表"的表现非常具体：

```
psycopg.errors.CheckViolation: new row for relation "ctf2_solve_attempts"
violates check constraint "ck_ctf2_solve_attempts_solve_attempt_profile_valid"
DETAIL: Failing row contains (ctf2try_…, ctf2chal_…, safe, 80, ctf2-sandbox-ir, …)
```

也就是说：**任务建得出来、界面上看着一切正常、点「开始复盘」才炸**，
而报错信息里那句 `safe, 80, ctf2-sandbox-ir` 看起来像是"策略本身有问题"，
不是"这条约束少了一个取值"。

这条是被 `test_ir_task_router.py::test_solve_on_local_task_does_not_need_platform`
抓出来的 —— 测试要验证的是"本地任务不需要平台账号"，结果撞在一条与
它毫不相关的约束上。这正是"正向路径测试比否定路径测试值钱"的例子：
后者只证明坏事没发生，前者会把你走过的每一块地基都踩一遍。

## 为什么不用 --autogenerate

同一个理由已经在 `c3d8f4b16a72` 里写过一次：`--autogenerate` **不比对
CHECK 表达式**。它看不到"模型里的 `values(KernelSandboxProfile)` 多了一个值"
与"数据库里那行字面量还是旧的"之间的差异，生成出来的迁移是空的。

## ⚠️ 约束名传裸名

`op.drop_constraint()` / `op.create_check_constraint()` 接受的是**裸名**
（`solve_attempt_profile_valid`），不是数据库全名
（`ck_ctf2_solve_attempts_solve_attempt_profile_valid`）—— 项目的
`naming_convention` 会自己套 `ck_<table>_` 前缀。传全名会得到：

```
constraint "ck_ctf2_solve_attempts_ck_ctf2_solve_attempts_solve_attempt_profile_valid"
of relation "ctf2_solve_attempts" does not exist
```

（这是 `c3d8f4b16a72` 里踩过的同一条，那次报错的对象是
`ctf2_challenges`。）
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.core.vocab import KernelSandboxProfile

revision: str = "e7b2c48d1a93"
down_revision: Union[str, None] = "d5a9e3c72f81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 表名与**裸**约束名、列名。三处必须同步。
_TABLE = "ctf2_solve_attempts"
_CONSTRAINT = "solve_attempt_profile_valid"
_COLUMN = "sandbox_profile"


def _allowed_values() -> str:
    """当前枚举的全部取值，拼成 SQL 的 IN 列表。

    不手写字符串常量：手写的那一刻它就开始与枚举漂移，而下一次加取值
    时又会漏掉这里 —— 这条迁移本身就是为了修一次这样的漂移。
    """
    return ", ".join(repr(member.value) for member in KernelSandboxProfile)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, f"{_COLUMN} IN ({_allowed_values()})"
    )


def downgrade() -> None:
    # 收紧前先把已经用了新值的行收回去 —— 否则 ALTER TABLE 校验既有行
    # 时直接报错，而那时人正急着回滚。
    #
    # ⚠️ 收到 `ctf2-sandbox`（基础镜像）而不是最接近的那个：
    #   `ctf2-sandbox-re` 里有反编译与调试工具，把一个 IR 任务记成
    #   "用逆向镜像跑的"会让事后复盘得到错误结论；记成基础镜像则只是
    #   "信息不全"。回滚路径上宁可少说，不说错的。
    op.execute(
        f"UPDATE {_TABLE} SET {_COLUMN} = 'ctf2-sandbox' "
        f"WHERE {_COLUMN} NOT IN ('ctf2-sandbox', 'ctf2-sandbox-re')"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"{_COLUMN} IN ('ctf2-sandbox', 'ctf2-sandbox-re')",
    )
