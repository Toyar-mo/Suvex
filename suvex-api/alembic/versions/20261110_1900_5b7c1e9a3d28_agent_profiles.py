"""`agent_profiles` -- 可选 Agent 实体（v1.83）。

`down_revision` = 2f6a9c4d8e31（credentials）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "5b7c1e9a3d28"
down_revision = "2f6a9c4d8e31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mcp_servers", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skills", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("tools", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_agent_profiles_key", "agent_profiles", ["key"], unique=True)
    op.create_index("ix_agent_profiles_builtin", "agent_profiles", ["builtin"])


def downgrade() -> None:
    op.drop_index("ix_agent_profiles_builtin", table_name="agent_profiles")
    op.drop_index("ix_agent_profiles_key", table_name="agent_profiles")
    op.drop_table("agent_profiles")
