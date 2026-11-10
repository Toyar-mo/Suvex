"""Agent 配置端点（v1.83）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agents import service
from app.agents.schemas import AgentProfileIn, AgentProfileOut, AgentProfilePatch
from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db
from app.core.errors import ApiError

router = APIRouter(prefix="/agents", tags=["Agent"])

ReadDep = Annotated[Identity, Depends(require_read)]
AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[AgentProfileOut], summary="Agent 清单")
def list_agents(_identity: ReadDep, db: DbDep) -> list[AgentProfileOut]:
    """首次访问会幂等播种内置角色。"""
    return [AgentProfileOut.from_row(row) for row in service.ensure_builtins(db)]


@router.post("", response_model=AgentProfileOut, status_code=201, summary="新建自定义 Agent")
def create_agent(
    payload: AgentProfileIn,
    _identity: AdminDep,
    db: DbDep,
) -> AgentProfileOut:
    return AgentProfileOut.from_row(
        service.create(
            db,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            mcp_servers=payload.mcpServers,
            skills=payload.skills,
            tools=payload.tools,
        )
    )


@router.patch("/{profileId}", response_model=AgentProfileOut, summary="修改自定义 Agent")
def update_agent(
    profileId: str,  # noqa: N803
    payload: AgentProfilePatch,
    _identity: AdminDep,
    db: DbDep,
) -> AgentProfileOut:
    fields = payload.model_dump(exclude_none=True)
    return AgentProfileOut.from_row(service.update(db, profileId, **fields))


@router.delete("/{profileId}", summary="删除自定义 Agent")
def delete_agent(
    profileId: str,  # noqa: N803
    _identity: AdminDep,
    db: DbDep,
) -> dict[str, bool]:
    if not service.delete(db, profileId):
        raise ApiError.not_found("Agent 不存在")
    return {"deleted": True}
