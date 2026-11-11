"""基准用例支持主动执行（v1.90）。

`benchmark_cases` 此前只能判定"已经跑过的扫描/题目"，于是"基准"永远在描述
过去。加三列之后，用例可以自己登记目标，`launch` 会真的建一次扫描。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e3f4a5b6c7d8"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("benchmark_cases", sa.Column("target", sa.String(length=512), nullable=True))
    op.add_column(
        "benchmark_cases",
        sa.Column("target_kind", sa.String(length=16), nullable=False, server_default="url"),
    )
    op.add_column(
        "benchmark_cases",
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # 历史行给空列表 —— NULL 会让 `create_scan` 的 skills 校验在运行期才炸。
    op.execute("UPDATE benchmark_cases SET skills = '[]'::jsonb WHERE skills IS NULL")
    op.alter_column(
        "benchmark_cases", "skills", nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def downgrade() -> None:
    op.drop_column("benchmark_cases", "skills")
    op.drop_column("benchmark_cases", "target_kind")
    op.drop_column("benchmark_cases", "target")
