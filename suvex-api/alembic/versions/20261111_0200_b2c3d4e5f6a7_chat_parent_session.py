"""`chat_sessions.parent_session_id`（v1.87，子代理分支）。

子代理会话不再作为顶层会话堆在侧栏，而是挂在父会话下面。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("parent_session_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_chat_sessions_parent_session_id",
        "chat_sessions",
        "chat_sessions",
        ["parent_session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_chat_sessions_parent_session_id",
        "chat_sessions",
        ["parent_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_parent_session_id", table_name="chat_sessions")
    op.drop_constraint(
        "fk_chat_sessions_parent_session_id", "chat_sessions", type_="foreignkey"
    )
    op.drop_column("chat_sessions", "parent_session_id")
