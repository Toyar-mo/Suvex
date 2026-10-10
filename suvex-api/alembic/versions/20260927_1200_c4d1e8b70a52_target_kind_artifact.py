"""scans.target_kind 允许 'artifact'（只有附件、没有地址）—— 任务 17

Revision ID: c4d1e8b70a52
Revises: b7c2f9a41d38
Create Date: 2026-09-27 12:00:00.000000+00:00

## 为什么要手工写这条迁移

与上一条（`service`）**同一个理由**，而且这一条更值得写清楚，
因为它已经是**第二次**踩在同一个坑上了：

`--autogenerate` **不比对 CHECK 约束的内容**。把 `TargetKind` 加上
`artifact` 之后，模型与数据库会分叉：模型认为允许 5 个取值，而库里的
`target_kind_valid` 仍然只放行 4 个 —— 于是**建一条逆向题的扫描会在
插入时被数据库打回**，而报错长得像"接口坏了"。

也就是说：这个 CHECK 约束**每加一个 TargetKind 就要跟着改一次**，
而工具不会提醒你。`core/vocab.py` 里 `TargetKind` 的 docstring 已经把
这条写成了"改枚举必须同时改三处"的第 3 条 —— 这里就是那第 3 条。

## 只改约束，不动数据

存量行的 `target_kind` 都在 `url/repo/path/service` 里，新约束是它的超集，
所以升级不需要回填，也不会失败。

⚠️ **降级会失败**，如果库里已经存在 `artifact` 的扫描行 —— 那时新约束
放不下它们。这是如实记录，不是遗漏：把一个已经存在的目标类型从约束里
摘掉，本来就该由人来决定那些行怎么办（改类型 / 删除），而不是让迁移
替他们猜。与上一条迁移同样的取舍。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "c4d1e8b70a52"
down_revision: str | None = "b7c2f9a41d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 `core.vocab.TargetKind` 逐字一致（这一版迁移当时的取值集合）
_KINDS_WITH_ARTIFACT = ("url", "repo", "path", "service", "artifact")
_KINDS_BEFORE = ("url", "repo", "path", "service")


def _values_clause(kinds: tuple[str, ...]) -> str:
    return "target_kind IN (" + ", ".join(repr(kind) for kind in kinds) + ")"


def upgrade() -> None:
    op.drop_constraint("target_kind_valid", "scans", type_="check")
    op.create_check_constraint(
        "target_kind_valid",
        "scans",
        _values_clause(_KINDS_WITH_ARTIFACT),
    )


def downgrade() -> None:
    op.drop_constraint("target_kind_valid", "scans", type_="check")
    op.create_check_constraint(
        "target_kind_valid",
        "scans",
        _values_clause(_KINDS_BEFORE),
    )
