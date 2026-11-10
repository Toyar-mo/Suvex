"""`pending_scope_items` -- 待批准的新目标（v1.82）。

发现的新目标不等于有权测试：本表只记录"待人工决定"，批准不会
自动改变正在跑的扫描。`down_revision` = 5d1a8c3f2e70（purple_rules）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8a3e5c7b1d42"
down_revision = "5d1a8c3f2e70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_scope_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="status_valid"
        ),
    )
    op.create_index("ix_pending_scope_scan", "pending_scope_items", ["scan_id"])
    op.create_index("ix_pending_scope_status", "pending_scope_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_pending_scope_status", table_name="pending_scope_items")
    op.drop_index("ix_pending_scope_scan", table_name="pending_scope_items")
    op.drop_table("pending_scope_items")
