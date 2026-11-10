"""`credentials` -- 凭据台账（v1.83）。

`down_revision` = 8a3e5c7b1d42（pending_scope_items）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "2f6a9c4d8e31"
down_revision = "8a3e5c7b1d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("scan_id", sa.String(length=64), nullable=True),
        sa.Column("finding_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="password"),
        sa.Column("username", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("secret_token", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("notes", sa.String(length=500), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('password', 'hash', 'ticket', 'key')", name="kind_valid"
        ),
    )
    op.create_index("ix_credentials_target", "credentials", ["target"])
    op.create_index("ix_credentials_scan", "credentials", ["scan_id"])


def downgrade() -> None:
    op.drop_index("ix_credentials_scan", table_name="credentials")
    op.drop_index("ix_credentials_target", table_name="credentials")
    op.drop_table("credentials")
