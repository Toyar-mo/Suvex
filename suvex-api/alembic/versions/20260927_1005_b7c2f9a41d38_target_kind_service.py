"""scans.target_kind 允许 'service'（裸 TCP 服务）—— 任务 15

Revision ID: b7c2f9a41d38
Revises: a3f81c6e0b27
Create Date: 2026-09-27 10:05:00.000000+00:00

## 为什么要手工写这条迁移

`--autogenerate` **不比对 CHECK 约束的内容**。把 `TargetKind` 加上
`service` 之后，模型与数据库会分叉：模型认为允许 4 个取值，而库里的
`target_kind_valid` 仍然只放行 3 个 —— 于是**创建 TCP 目标的扫描会
在插入时被数据库打回**，而看起来像"接口坏了"。

本项目在 `findings.status` 上真踩过同一类：当时扩到 6 个状态，
改了两处、漏了约束，于是新状态写不进去。所以这条迁移不是形式。

## 只改约束，不动数据

存量行的 `target_kind` 都在 `url/repo/path` 里，新约束是它的超集，
所以升级不需要回填、也不会失败。

⚠️ **降级会失败**，如果库里已经存在 `service` 的扫描行 —— 那时新约束
放不下它们。这是如实记录，不是遗漏：把一个已经存在的目标类型从
约束里摘掉，本来就该由人来决定那些行怎么办（改类型 / 删除），
而不是让迁移替他们猜。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "b7c2f9a41d38"
down_revision: str | None = "a3f81c6e0b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 `core.vocab.TargetKind` 逐字一致（这一版迁移当时的取值集合）
_KINDS_WITH_SERVICE = ("url", "repo", "path", "service")
_KINDS_BEFORE = ("url", "repo", "path")


def _values_clause(kinds: tuple[str, ...]) -> str:
    return "target_kind IN (" + ", ".join(repr(kind) for kind in kinds) + ")"


def upgrade() -> None:
    op.drop_constraint("target_kind_valid", "scans", type_="check")
    op.create_check_constraint(
        "target_kind_valid",
        "scans",
        _values_clause(_KINDS_WITH_SERVICE),
    )


def downgrade() -> None:
    op.drop_constraint("target_kind_valid", "scans", type_="check")
    op.create_check_constraint(
        "target_kind_valid",
        "scans",
        _values_clause(_KINDS_BEFORE),
    )
