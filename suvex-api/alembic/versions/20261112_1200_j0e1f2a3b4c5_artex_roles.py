"""ARTeX 角色替换：会话人格与内置 Agent 清单。

Revision ID: j0e1f2a3b4c5
Revises: i9d0e1f2a3b4
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "j0e1f2a3b4c5"
down_revision = "i9d0e1f2a3b4"
branch_labels = None
depends_on = None

ROLES = (
    "goals",
    "planner",
    "mainagent",
    "worker",
    "worker.intranet",
    "auto",
    "pentest",
    "reporter",
    "retester",
    "verifier",
)


def upgrade() -> None:
    # 旧人格（recon/binary/...）不在新集合里；先归零再换约束。
    op.execute(
        sa.text("UPDATE chat_sessions SET persona = NULL WHERE persona IS NOT NULL")
    )
    op.drop_constraint("chat_session_persona_valid", "chat_sessions", type_="check")
    op.create_check_constraint(
        "chat_session_persona_valid",
        "chat_sessions",
        "persona IS NULL OR persona IN "
        f"({', '.join(repr(role) for role in ROLES)})",
    )
    op.execute(
        sa.text(
            "DELETE FROM agent_profiles WHERE builtin = true AND key NOT IN "
            f"({', '.join(repr(role) for role in ROLES)})"
        )
    )


def downgrade() -> None:
    op.drop_constraint("chat_session_persona_valid", "chat_sessions", type_="check")
    op.create_check_constraint(
        "chat_session_persona_valid",
        "chat_sessions",
        "persona IS NULL OR persona IN ("
        "'default','recon','pentest','binary','postex','container','forensics','api','cloud','ctf','aillm'"
        ")",
    )
