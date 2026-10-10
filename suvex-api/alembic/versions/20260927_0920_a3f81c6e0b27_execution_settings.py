"""sandbox_config 加执行类三件套（并发 / 回合上限 / 扫描深度）—— 任务 13

Revision ID: a3f81c6e0b27
Revises: d7beeb9712a9
Create Date: 2026-09-27 09:20:00.000000+00:00

## 为什么这三列**可空**（本次迁移最要紧的一处判断）

`NULL` 在这里不是"没数据"，而是一个明确语义：**用户没在设置页覆盖过，
跟着 env 走**。

所以它们既不能 `nullable=False`，也不能带 `server_default`：那样会在
单例行建立的那一刻把配置**冻成一个代码常量**，于是部署时设的
`SUPEX_MAX_CONCURRENT_SCANS=3` 被静默无视 —— 而用户以为自己设的生效了。
（上一版迁移 `d7beeb9712a9` 给 `scans.goal` 用 `server_default` 是对的，
那是因为它是一个**必填**的业务字段；这里的语义完全不同。）

## CHECK 约束要手工补

`--autogenerate` **不比对 CHECK 约束的内容** —— 本项目已经踩过一次
（`ck_findings_status_valid` 当时只允许 4 个状态，而代码已经写 6 个）。
`models.py` 里那条 `scan_mode_valid` 是真实存在的约束，不补的话模型与库
会分叉，下一次 autogenerate 又会想"该建"。

取值与 `core.vocab.ScanMode` 逐字一致（那个字面量是"这一版迁移当时的
取值集合"，本来就不该随后续版本漂移）。

## 只加列，不动数据

存量实例里那些行的三列都会是 `NULL` = "用户没覆盖过"，
行为与迁移前**完全一致**（继续读 env）。所以这次迁移不需要回填，
也不改变任何现有扫描的执行参数。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "a3f81c6e0b27"
down_revision: str | None = "d7beeb9712a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 `core.vocab.ScanMode` 逐字一致
_SCAN_MODES = ("quick", "standard", "deep")


def upgrade() -> None:
    # ① 并发上限。可空 = 未覆盖（见模块 docstring）。
    op.add_column(
        "sandbox_config",
        sa.Column("max_concurrent_scans", sa.Integer(), nullable=True),
    )
    # ② 单个 agent 的回合上限（strix 的 --max-turns）。
    op.add_column(
        "sandbox_config",
        sa.Column("scan_max_turns", sa.Integer(), nullable=True),
    )
    # ③ 扫描深度（strix 的 --scan-mode）。
    op.add_column(
        "sandbox_config",
        sa.Column("scan_mode", sa.String(length=16), nullable=True),
    )

    # ④ 手工补 CHECK（autogenerate 不比对约束内容）。
    #    ⚠️ 必须放行 NULL —— 写成 `scan_mode IN (...)` 会把
    #       "没设过"这个正常状态直接打回。
    op.create_check_constraint(
        "scan_mode_valid",
        "sandbox_config",
        "scan_mode IS NULL OR scan_mode IN ("
        + ", ".join(repr(v) for v in _SCAN_MODES)
        + ")",
    )


def downgrade() -> None:
    op.drop_constraint("scan_mode_valid", "sandbox_config", type_="check")
    op.drop_column("sandbox_config", "scan_mode")
    op.drop_column("sandbox_config", "scan_max_turns")
    op.drop_column("sandbox_config", "max_concurrent_scans")
