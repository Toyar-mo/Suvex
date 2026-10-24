"""`scan_schedules` 表：定时扫描计划（v0.85）。

## 为什么加这张表

v0.85 的资产视图回答了「我有哪些目标、每个上次是什么时候查的」——
但它只能告诉你**有没有人管**，不能**替你管**。用户看到「40 天没被检查」
之后的下一步动作，就是手动再点一次扫描。这张表把那一步变成自动的。

## 为什么是独立表，不在 `scans` 上加列

`scans` 是**已发生的事实**（一次扫描、一条记录、可归档可删除）。
计划是**未来的意图** —— 它没有开始时间、没有结束时间、没有结果，
而且一条计划会对应**很多次**扫描。把两者揉在一起会出现
「`status` 对一个计划意味着什么」这种答不上来的问题。

## 为什么 `last_scan_id` 不设外键

扫描会被清理（归档、删除）。而计划不该因为"它所建的那次扫描被删了"
就失去历史 —— 那会让界面显示成"从来没跑过"，用户以为它坏了。
所以在 `models.py` 里它是一个**不带头键的身份**：存 id，扫不到就不显示
"上次结果"这一栏，但 `last_run_at` 仍如实保留（两个字段刻意分开，
见 `app/schedules/models.py` 的说明）。

## 枚举值与 CHECK 约束

`target_kind` / `goal` 的取值集合在这里**抄一遍**而不是 import 模型 ——
迁移是历史快照，必须永远生成当初那张表。import 模型会让将来改模型时
这个迁移悄悄生成一个不一样的表（alembic 最经典的坑）。

Revision ID: c7b1e4a90f38
Revises: a9d4f2c71e83
Create Date: 2026-10-24 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7b1e4a90f38"
down_revision = "a9d4f2c71e83"
branch_labels = None
depends_on = None

#: ⚠️ 与 `app/core/vocab.py` 的 `TargetKind` / `ScanGoal`、以及
#: `app/schedules/models.py` 的上下界必须逐字一致。
_TARGET_KINDS = ("url", "repo", "path", "service", "artifact")
_GOALS = ("findVulns", "captureFlag", "verifyFix")
_MIN_INTERVAL_MINUTES = 5
_MAX_INTERVAL_MINUTES = 365 * 24 * 60


def upgrade() -> None:
    op.create_table(
        "scan_schedules",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("goal", sa.String(length=32), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # 停用的计划没有下次时间（见下方的 CHECK 约束）。
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        # ⚠️ 刻意**不设**外键：扫描会被清理，计划不该因此失去历史。
        sa.Column("last_scan_id", sa.String(length=64), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_label", sa.String(length=200), nullable=True),
        # 与 `app/core/columns.py::JsonColumn` 同类型：PG 上 JSONB、其余 JSON。
        sa.Column(
            "skills",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("brief", sa.Text(), nullable=True),
        # ── 授权留痕（与 `scans.authorization_*` 同一套口径） ──
        sa.Column("authorized_by", sa.String(length=64), nullable=True),
        sa.Column("authorization_statement", sa.Text(), nullable=True),
        sa.Column("authorization_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            f"target_kind IN ({', '.join(repr(v) for v in _TARGET_KINDS)})",
            name="schedule_target_kind_valid",
        ),
        sa.CheckConstraint(
            f"goal IN ({', '.join(repr(v) for v in _GOALS)})",
            name="schedule_goal_valid",
        ),
        sa.CheckConstraint(
            f"interval_minutes >= {_MIN_INTERVAL_MINUTES} AND interval_minutes <= {_MAX_INTERVAL_MINUTES}",
            name="schedule_interval_range",
        ),
        # ★ 停用与"没有下次时间"必须同时成立或同时不成立。
        #
        # 这条约束挡住的是一个很具体的坏状态：计划被停用但 `next_run_at`
        # 还留在过去 —— 调度器查的是 `enabled AND next_run_at <= now`，
        # 于是它不会扫（好），但界面上会显示"下次扫描：昨天"（坏，
        # 用户会以为它卡住了）。写成 CHECK 就没有"某个代码路径忘了清"。
        sa.CheckConstraint(
            "(enabled = false AND next_run_at IS NOT NULL) = false",
            name="schedule_disabled_has_no_next_run",
        ),
    )
    # 调度器每 tick 只查「启用且到点」的那些 —— 这条索引就是为它建的。
    op.create_index("ix_scan_schedules_due", "scan_schedules", ["enabled", "next_run_at"])
    op.create_index("ix_scan_schedules_target", "scan_schedules", ["target"])


def downgrade() -> None:
    op.drop_index("ix_scan_schedules_target", table_name="scan_schedules")
    op.drop_index("ix_scan_schedules_due", table_name="scan_schedules")
    op.drop_table("scan_schedules")
