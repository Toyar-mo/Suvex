"""沙箱镜像名 `ctf2-sandbox*` → `suvex-sandbox*`

Revision ID: f1a9c4e83d27
Revises: e8c5f1a3d927
Create Date: 2026-10-22 17:00

## 这条迁移做两件事

1. **改数据**：四张表里存的旧镜像名（`ctf2-sandbox*`）全部改写成新名
   （`suvex-sandbox*`）。
2. **重建 CHECK**：四条 `..._sandbox_profile_valid` 约束的取值集合从
   "旧 3 值"扩成"新 3 值 + 旧 3 值"。

## 为什么 CHECK 要同时接受新旧两值

代码里 `KernelSandboxProfile` 现在有 6 个成员（3 新 + 3 旧）。旧名保留在
枚举里**纯粹为了过渡窗口**：迁移已跑、但某台部署机的镜像还没 `docker tag`
出新名，或代码回滚到旧版时，库里那一行字面量不该把整页搞成 500。
`values()` 生成的表达式天然就是 6 个值，所以这里直接用 `_allowed_values()`。

## 为什么数据先于约束

先 UPDATE 再重建 CHECK：新版 CHECK 是旧版的**超集**（6 ⊇ 3），其实顺序
反过来也不会违规；但先改数据能让"新值真的落到库里"成为迁移的既成事实，
回滚时才有东西可回。这里两个顺序都安全，选先改数据。

## 四张表 / 四条约束 / 各自的列

| 表 | 裸约束名 | 列 |
| --- | --- | --- |
| `suvex_challenges` | `sandbox_profile_valid` | `sandbox_profile` |
| `suvex_environments` | `sandbox_profile_valid` | `sandbox_profile` |
| `suvex_solve_attempts` | `solve_attempt_profile_valid` | `sandbox_profile` |
| `suvex_config` | `default_sandbox_profile_valid` | `default_sandbox_profile` |

⚠️ 约束名传**裸名**，让 `naming_convention` 自己加 `ck_<table>_` 前缀。
（传全名会得到 `constraint "ck_..._ck_..." does not exist` —— 见
`e7b2c48d1a93` 的 docstring。）

## 升级路径上的镜像本身

代码只认名字，镜像本身要在部署机上打新 tag：

```
docker tag ctf2-sandbox:latest    suvex-sandbox:latest
docker tag ctf2-sandbox-re:latest suvex-sandbox-re:latest
docker tag ctf2-sandbox-ir:latest suvex-sandbox-ir:latest
```

`docker tag` 是秒级、不重建。旧 tag 保留不删 —— 回滚代码时还要用。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.core.vocab import KernelSandboxProfile

revision: str = "f1a9c4e83d27"
down_revision: str | None = "e8c5f1a3d927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (表名, **裸**约束名, 列名)。四处必须同步。
_SITES: tuple[tuple[str, str, str], ...] = (
    ("suvex_challenges", "sandbox_profile_valid", "sandbox_profile"),
    ("suvex_environments", "sandbox_profile_valid", "sandbox_profile"),
    ("suvex_solve_attempts", "solve_attempt_profile_valid", "sandbox_profile"),
    ("suvex_config", "default_sandbox_profile_valid", "default_sandbox_profile"),
)

#: 旧名 → 新名（与 `app/core/vocab.LEGACY_SANDBOX_PROFILE_MAP` 同一份映射，
#: 但迁移里**写死字面量**：迁移一旦建好就该固化，不该跟着未来枚举变）。
_RENAME: dict[str, str] = {
    "ctf2-sandbox": "suvex-sandbox",
    "ctf2-sandbox-re": "suvex-sandbox-re",
    "ctf2-sandbox-ir": "suvex-sandbox-ir",
}


def _allowed_values() -> str:
    """当前枚举全部取值（新 3 + 旧 3），拼成 SQL 的 IN 列表。"""
    return ", ".join(repr(member.value) for member in KernelSandboxProfile)


def upgrade() -> None:
    # ★ 顺序：**先扩 CHECK，再改数据**。
    #
    # 反过来会在这一步炸：
    #   CheckViolation: new row for relation "suvex_config" violates check
    #   constraint "ck_suvex_config_default_sandbox_profile_valid"
    # 因为旧约束只认 3 个旧值，写新值当场被它拦下（约束是**写入时**校验的）。
    # 第一次就是先改数据，真跑才发现。
    allowed = _allowed_values()
    for table, constraint, column in _SITES:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint, table, f"{column} IN ({allowed})"
        )
    # 2) 改数据：旧名 → 新名。
    for _table, _constraint, column in _SITES:
        for old, new in _RENAME.items():
            op.execute(
                f"UPDATE {_table} SET {column} = {new!r} WHERE {column} = {old!r}"
            )


def downgrade() -> None:
    # 1) 数据改回旧名（新 → 旧）。
    for table, _constraint, column in _SITES:
        for old, new in _RENAME.items():
            op.execute(
                f"UPDATE {table} SET {column} = {old!r} WHERE {column} = {new!r}"
            )
    # 2) 约束收回到旧 3 值。
    old_allowed = ", ".join(repr(old) for old in _RENAME)
    for table, constraint, column in _SITES:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint, table, f"{column} IN ({old_allowed})"
        )
