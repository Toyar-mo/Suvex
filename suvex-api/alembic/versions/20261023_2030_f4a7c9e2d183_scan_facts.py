"""`scan_facts` 表：扫描过程的项目黑板（v0.79）。

## 为什么加这张表

v0.77 验证扫描实测：`run_shell` 238 次，`record_coverage` 只有 **2 次**，
`add_idea` / `add_fact` 各 **0 次** —— 工具都在、提示词写了、测试全绿，
模型就是不用。

v0.79 实验 A 把「当前状态」每轮强制推进 root 的 system prompt
（`multiagent.render_live_state`），解决了"看不见"。但那是**内存态**：
进程重启就没了，扫描结束就没了，**跨扫描什么也留不下**。

这张表学 CyberStrikeAI 的 `project_facts`：把「已确认 / 已排除 / 约束」
落库，下次对同一目标扫描时能继承上次的结论 —— 尤其是 `ruled-out`
（试过但走不通），它的失效速度最慢、重复验证成本最高。

## 为什么是独立表，不塞进 findings

`findings` 是**对外交付的漏洞条目**（严重度、状态流转、SLA、给人看）；
`scan_facts` 是**过程中的认知**（包括"这个点走不通"）。

把 negative 塞进 findings 会让漏洞统计被"我没找到"污染 —— 这正是
v0.75 `should_enter_findings` 把 L0 挡在外面要防的事。反过来把
findings 塞进黑板会让它被已交付内容撑爆。

## 为什么 `scan_id` 是 SET NULL 而不是 CASCADE

扫描记录被删时，事实仍有参考价值。`ruled-out` 尤其如此：
"上次扫这个目标时 `/api/v1/scans` 打不通" 是关于**目标**的知识，
不是关于**那次扫描**的知识。所以扫描没了，事实留着。
`scan_id = NULL` = "写它的时候那次扫描已不在库"。

## 为什么边先放 JSON 列而不是独立表

独立边表的价值是"按关系类型反向查"（哪些事实 enables 了这条 RCE）。
当前唯一读者是渲染索引块（把边上的 key 列出来），JSON 够用。
改成表是一次迁移的事，而现在加表只会让模型多一套 UPSERT 要学。

Revision ID: f4a7c9e2d183
Revises: b3e6a1d94f27
Create Date: 2026-10-23 20:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f4a7c9e2d183"
down_revision = "b3e6a1d94f27"
branch_labels = None
depends_on = None

#: ⚠️ 与 `app/facts/models.py` 的 `FACT_CATEGORIES` / `FACT_CONFIDENCES`
#: 必须逐字一致。**故意在这里抄一遍而不是 import**：迁移文件是历史快照，
#: 它必须永远生成当初那张表；import 模型会让将来改模型时，这个迁移
#: 悄悄生成一个不一样的表（那是 alembic 最经典的坑）。
_CATEGORIES = ("endpoint", "credential", "surface", "ruled-out", "constraint", "note")
_CONFIDENCES = ("confirmed", "tentative", "deprecated")


def upgrade() -> None:
    op.create_table(
        "scan_facts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=True),
        sa.Column("fact_key", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("evidence_excerpt", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("trust_level", sa.String(length=4), nullable=True),
        # 与 `app/core/columns.py::JsonColumn` 同类型：PG 上 JSONB、其余 JSON。
        sa.Column(
            "edges",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            f"confidence IN ({', '.join(repr(c) for c in _CONFIDENCES)})",
            name="scan_fact_confidence_valid",
        ),
        sa.CheckConstraint(
            f"category IN ({', '.join(repr(c) for c in _CATEGORIES)})",
            name="scan_fact_category_valid",
        ),
        sa.UniqueConstraint("scan_id", "fact_key", name="uq_scan_facts_scan_key"),
    )
    op.create_index("ix_scan_facts_scan_id", "scan_facts", ["scan_id"], unique=False)
    op.create_index("ix_scan_facts_target", "scan_facts", ["target"], unique=False)
    op.create_index("ix_scan_facts_category", "scan_facts", ["category"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_scan_facts_category", table_name="scan_facts")
    op.drop_index("ix_scan_facts_target", table_name="scan_facts")
    op.drop_index("ix_scan_facts_scan_id", table_name="scan_facts")
    op.drop_table("scan_facts")
