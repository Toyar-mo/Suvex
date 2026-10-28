"""chat: 新建 `chat_turn_runs` —— 长任务持久化（v1.19）

## 这个迁移在加什么

一张记录「这一轮对话跑到哪儿了」的表。用户的原话是「没有长任务持久化啊」：
改造前那一轮的进度（第几轮、纠偏用了几次、跑过几次工具）只活在
`run_turn` 的 Python 栈帧里，进程重启/请求断开/抛异常就全丢，界面上只剩
半句话。有了这张表，后台 resume 循环才能"接着跑"。

## 为什么新表而不是给 `chat_messages` 加列

消息是**给用户看的产物**，执行状态是**机器状态**。两者生命周期不同：
崩溃时恰好是"消息落了几条、进度停在哪儿"不一致的那一刻，混在一张表里
会让「该渲染几条」和「跑到第几轮」互相污染。

## 为什么 `status` 要 `interrupted`

进程重启时内存里的栈帧一起没了，无法区分"正在跑"和"跑到一半被打断"。
启动时把遗留的 `running` 一律改判 `interrupted`，让"谁该被接手"有确定
判据，而不是去猜 `heartbeat_at` 有多旧。

## 索引

- `(status, heartbeat_at)`：后台每 20 秒扫一次"还没结束且心跳停了"的行。
- `(session_id, started_at)`：查某个会话的执行历史（排障用）。

## 兼容性

新表，存量数据无影响。老会话没有历史行 —— resume 循环只认"存在且未结束"
的行，所以它们不会被误接手。

## 回滚

drop 表。回滚后长任务持久化失效，退回到"栈帧丢了就丢了"的行为；
对话消息本身不受影响。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5e2f8b31c47"
down_revision = "c4d9e1a72b38"
branch_labels = None
depends_on = None

TABLE = "chat_turn_runs"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nudges_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tools_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resume_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','done','failed','interrupted')",
            name="chat_turn_run_status_valid",
        ),
    )
    op.create_index(
        "ix_chat_turn_runs_status_heartbeat", TABLE, ["status", "heartbeat_at"]
    )
    op.create_index(
        "ix_chat_turn_runs_session", TABLE, ["session_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turn_runs_session", table_name=TABLE)
    op.drop_index("ix_chat_turn_runs_status_heartbeat", table_name=TABLE)
    op.drop_table(TABLE)
