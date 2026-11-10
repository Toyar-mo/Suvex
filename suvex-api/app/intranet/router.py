"""内网会话台账端点（v1.83）。

只登记，不连接、不执行 —— 执行器接入前不提供"一键命令"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read, require_scan
from app.core.db import get_db
from app.core.errors import ApiError
from app.intranet import service
from app.intranet.schemas import SessionIn, SessionOut, SessionPatch

router = APIRouter(prefix="/intranet/sessions", tags=["内网会话"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[SessionOut], summary="会话台账")
def list_sessions(_identity: ReadDep, db: DbDep) -> list[SessionOut]:
    return [SessionOut.from_row(row) for row in service.list_sessions(db)]


@router.post("", response_model=SessionOut, status_code=201, summary="登记立足点")
def create_session(payload: SessionIn, identity: ScanDep, db: DbDep) -> SessionOut:
    return SessionOut.from_row(
        service.create(
            db,
            kind=payload.kind,
            host=payload.host,
            username=payload.username,
            secret=payload.secret,
            notes=payload.notes,
            status=payload.status,
            created_by=identity.user_id or "",
        )
    )


@router.patch("/{sessionId}", response_model=SessionOut, summary="更新会话状态/备注")
def update_session(
    sessionId: str,  # noqa: N803
    payload: SessionPatch,
    _identity: ScanDep,
    db: DbDep,
) -> SessionOut:
    return SessionOut.from_row(
        service.update(db, sessionId, **payload.model_dump(exclude_none=True))
    )


@router.delete("/{sessionId}", summary="删除会话记录")
def delete_session(sessionId: str, _identity: ScanDep, db: DbDep) -> dict[str, bool]:  # noqa: N803
    if not service.delete(db, sessionId):
        raise ApiError.not_found("会话不存在")
    return {"deleted": True}
