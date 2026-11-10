"""`activity_events.source_row_id` INTEGER → BIGINT（v1.86）。

自研引擎的 `events.jsonl` 用纳秒时间戳做 `source_row_id`，
1.79e18 超出 32 位 INTEGER；导入器插入失败，活动页停在旧事件。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "1a7c9e3b5d48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("activity_events") as batch:
        batch.alter_column(
            "source_row_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("activity_events") as batch:
        batch.alter_column(
            "source_row_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
