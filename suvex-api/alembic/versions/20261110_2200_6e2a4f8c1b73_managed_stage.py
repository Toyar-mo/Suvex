"""`managed_stage` -- 受管暂存（v1.83）。

`down_revision` = 3c6e8b1f5a92（llm_retry_config）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "6e2a4f8c1b73"
down_revision = "3c6e8b1f5a92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_stage",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False, server_default="text/plain"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_managed_stage_token", "managed_stage", ["token"], unique=True)
    op.create_index("ix_managed_stage_expires", "managed_stage", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_managed_stage_expires", table_name="managed_stage")
    op.drop_index("ix_managed_stage_token", table_name="managed_stage")
    op.drop_table("managed_stage")
