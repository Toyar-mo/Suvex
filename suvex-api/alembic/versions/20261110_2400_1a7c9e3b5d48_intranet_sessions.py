"""`intranet_sessions` -- 内网会话台账（v1.83）。

`down_revision` = 8f1c3d5e7a24（chat auto approve）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "1a7c9e3b5d48"
down_revision = "8f1c3d5e7a24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intranet_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=512), nullable=False),
        sa.Column("username", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("secret_token", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("notes", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('webshell', 'reverse', 'ssh')", name="kind_valid"),
        sa.CheckConstraint("status IN ('alive', 'dead', 'unknown')", name="status_valid"),
    )
    op.create_index("ix_intranet_sessions_created_by", "intranet_sessions", ["created_by"])
    op.create_index("ix_intranet_sessions_status", "intranet_sessions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_intranet_sessions_status", table_name="intranet_sessions")
    op.drop_index("ix_intranet_sessions_created_by", table_name="intranet_sessions")
    op.drop_table("intranet_sessions")
