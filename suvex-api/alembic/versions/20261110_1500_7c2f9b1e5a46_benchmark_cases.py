"""`benchmark_cases` / `benchmark_runs` -- 评测用例与判定记录（v1.81）。

## 为什么不把用例做成配置 JSON

用例需要被逐条删改、按 suite 查询、与历史 run 关联。塞进一张配置表
的 JSON 里会立刻需要"改第 3 条"这种字符串手术。

`down_revision` = 9e5c1d2a4f68（quality_snapshots）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "7c2f9b1e5a46"
down_revision = "9e5c1d2a4f68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_cases",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("suite", sa.String(length=80), nullable=False, server_default="default"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=True),
        sa.Column("scan_id", sa.String(length=64), nullable=True),
        sa.Column("expect", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_benchmark_cases_suite", "benchmark_cases", ["suite"])

    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_benchmark_runs_case", "benchmark_runs", ["case_id"])
    op.create_index("ix_benchmark_runs_collected", "benchmark_runs", ["collected_at"])


def downgrade() -> None:
    op.drop_index("ix_benchmark_runs_collected", table_name="benchmark_runs")
    op.drop_index("ix_benchmark_runs_case", table_name="benchmark_runs")
    op.drop_table("benchmark_runs")
    op.drop_index("ix_benchmark_cases_suite", table_name="benchmark_cases")
    op.drop_table("benchmark_cases")
