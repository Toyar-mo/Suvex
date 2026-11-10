"""`quality_snapshots` -- 评测快照（v1.81）。

## 为什么是一张新表

质量指标本来就能从 scans / findings / challenges 算出来，但那只能回答
"现在是多少"。要回答"这周比上周好了没有"，需要把某个时刻的截面**留存**。
存原始运行记录会让这张表变成第二份事实源，所以只存指标 JSON。

`down_revision` = 4b8d2e6f9a13（model_routes）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "9e5c1d2a4f68"
down_revision = "4b8d2e6f9a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_snapshots",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("label", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_quality_snapshots_captured",
        "quality_snapshots",
        ["captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_quality_snapshots_captured", table_name="quality_snapshots")
    op.drop_table("quality_snapshots")
