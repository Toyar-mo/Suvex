"""`agent_profiles.kind` -- 区分流程角色与聊天人格（v1.83）。

`down_revision` = 5b7c1e9a3d28（agent_profiles）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9d4b6a2c7f15"
down_revision = "5b7c1e9a3d28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="workflow"),
    )
    op.create_index("ix_agent_profiles_kind", "agent_profiles", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_agent_profiles_kind", table_name="agent_profiles")
    op.drop_column("agent_profiles", "kind")
