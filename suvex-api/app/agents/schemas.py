"""Agent 配置的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.agents.models import AgentProfile
from app.core.schemas import CamelModel, StrictModel


class AgentProfileIn(StrictModel):
    key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    mcpServers: list[str] = []
    skills: list[str] = []
    tools: list[str] = []


class AgentProfilePatch(StrictModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    mcpServers: list[str] | None = None
    skills: list[str] | None = None
    tools: list[str] | None = None


class AgentProfileOut(CamelModel):
    id: str
    key: str
    name: str
    description: str
    builtin: bool
    enabled: bool
    mcpServers: list[str]
    skills: list[str]
    tools: list[str]
    createdAt: str
    updatedAt: str

    @classmethod
    def from_row(cls, row: AgentProfile) -> AgentProfileOut:
        return cls(
            id=row.id,
            key=row.key,
            name=row.name,
            description=row.description,
            builtin=row.builtin,
            enabled=row.enabled,
            mcpServers=list(row.mcp_servers or []),
            skills=list(row.skills or []),
            tools=list(row.tools or []),
            createdAt=row.created_at.isoformat() if row.created_at else "",
            updatedAt=row.updated_at.isoformat() if row.updated_at else "",
        )
