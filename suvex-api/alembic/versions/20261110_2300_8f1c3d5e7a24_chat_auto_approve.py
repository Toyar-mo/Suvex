"""`chat_sessions.auto_approve_until` -- 审批自动放行（v1.83）。

`down_revision` = 6e2a4f8c1b73（managed_stage）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8f1c3d5e7a24"
down_revision = "6e2a4f8c1b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("auto_approve_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "auto_approve_until")
