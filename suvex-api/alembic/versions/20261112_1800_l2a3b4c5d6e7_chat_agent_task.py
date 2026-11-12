"""chat_sessions.agent_task_id：worker/planner 会话挂在执行图任务上。

Revision ID: l2a3b4c5d6e7
Revises: k1f2a3b4c5d6
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "l2a3b4c5d6e7"
down_revision = "k1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("agent_task_id", sa.String(64), nullable=True))
    op.create_index("ix_chat_sessions_agent_task", "chat_sessions", ["agent_task_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_agent_task", table_name="chat_sessions")
    op.drop_column("chat_sessions", "agent_task_id")
