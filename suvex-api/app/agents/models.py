"""可配置 Agent 实体（v1.83）。

与 `events.Agent`（一次扫描里的执行者）**不是一回事**：这里存的是
"可选角色"的配置（提示词/工具/Skill/MCP 绑定的展示面），
用于编排与管理；执行器是否读取这些绑定由接入层决定。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        Index("ix_agent_profiles_key", "key", unique=True),
        Index("ix_agent_profiles_builtin", "builtin"),
        Index("ix_agent_profiles_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: `workflow`（规划/执行等执行器角色）或 `persona`（聊天页可选角色）。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="workflow")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mcp_servers: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    skills: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    tools: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )
