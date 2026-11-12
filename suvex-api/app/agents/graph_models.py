"""Agent 执行图：任务 → 目标 → 意图 → 运行记录（P1）。

这是 ARTeX `task/goals/intents` 语义在 Suvex 的落地。P1 只做**图的持久化
与状态机**：谁创建、谁领取、谁写回、何时算完成。planner/worker 的自动
唤醒在 P2 接；当前由角色通过聊天工具驱动，但状态与归属已经是真的。

不把目标/意图塞进 `chat_todos`：todo 是"这一轮要做什么"的线性清单，
没有负责人、依赖、领取与完成判定；两者混用会让任务图看起来像计划表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

TASK_STATUSES = ("planning", "running", "completed", "failed", "cancelled")
GOAL_STATUSES = ("open", "met", "abandoned")
INTENT_STATUSES = ("open", "running", "done", "exhausted", "blocked", "stopped")


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_owner_updated", "created_by", "updated_at"),
        Index("ix_agent_tasks_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 操作约束（授权范围、禁止项）。P1 只存储与展示，执行层在 P2 接。
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planning")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AgentGoal(Base):
    __tablename__ = "agent_goals"
    __table_args__ = (Index("ix_agent_goals_task_seq", "task_id", "seq"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )


class AgentIntent(Base):
    __tablename__ = "agent_intents"
    __table_args__ = (
        Index("ix_agent_intents_task_status", "task_id", "status"),
        Index("ix_agent_intents_goal", "goal_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_goals.id", ondelete="SET NULL"), nullable=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: 领取这条意图的对话会话（worker 的上下文）。
    worker_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False
    )
    intent_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_intents.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


__all__ = [
    "AgentGoal",
    "AgentIntent",
    "AgentRun",
    "AgentTask",
    "GOAL_STATUSES",
    "INTENT_STATUSES",
    "TASK_STATUSES",
]
