"""scans.agent_task_id：执行图任务与扫描关联。

Revision ID: m3b4c5d6e7f8
Revises: l2a3b4c5d6e7
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "m3b4c5d6e7f8"
down_revision = "l2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scans", sa.Column("agent_task_id", sa.String(64), nullable=True))
    op.create_index("ix_scans_agent_task", "scans", ["agent_task_id"])


def downgrade() -> None:
    op.drop_index("ix_scans_agent_task", table_name="scans")
    op.drop_column("scans", "agent_task_id")
