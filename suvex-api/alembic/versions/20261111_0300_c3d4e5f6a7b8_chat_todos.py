"""`chat_todos`：会话级计划（v1.89）。

对话侧的"计划"此前只存在于模型文本里，引擎靠启发式句子判断（"接下来我
打算…"）来识别计划句 —— 那是猜。这张表把计划变成可查询的状态：模型自己
写、自己改状态，引擎每轮把它渲染进系统提示词。

与扫描侧的 `create_todo` 不是同一套：那个是单次扫描内的私有待办（内存态，
随进程消失）；这张表属于**对话**，跨轮、跨重启都在。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_todos",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        # `server_default` 必须与模型里的 `default` 对齐，否则 `alembic check`
        # 会永远想生成一条 `modify_default`（与 `chat_sessions.message_count`
        # 同一类漂移）。
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "seq", name="chat_todo_session_seq"),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'done')",
            name="chat_todo_status_valid",
        ),
    )
    op.create_index("ix_chat_todos_session_seq", "chat_todos", ["session_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_chat_todos_session_seq", table_name="chat_todos")
    op.drop_table("chat_todos")
