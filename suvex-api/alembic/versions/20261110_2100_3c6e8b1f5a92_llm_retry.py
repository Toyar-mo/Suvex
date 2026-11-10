"""`llm_retry_config` -- 模型调用重试策略（v1.83）。

`down_revision` = 9d4b6a2c7f15（agent_profiles.kind）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "3c6e8b1f5a92"
down_revision = "9d4b6a2c7f15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_retry_config",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("connect_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("connect_interval_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("empty_retries", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("empty_interval_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stream_retries", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("stream_interval_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breaker_failures", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("breaker_interval_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breaker_cooldown_s", sa.Integer(), nullable=False, server_default="60"),
    )


def downgrade() -> None:
    op.drop_table("llm_retry_config")
