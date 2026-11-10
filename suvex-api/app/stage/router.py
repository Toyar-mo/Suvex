"""受管暂存端点（v1.83）。

| 方法 | 路径 | 鉴权 |
| --- | --- | --- |
| POST | `/stage/text` | scan |
| GET | `/stage/s/{token}/{name}` | public（token 即凭据） |
| DELETE | `/stage/{id}` | scan |
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_scan
from app.core.db import get_db
from app.core.errors import ApiError
from app.stage import service
from app.stage.schemas import StageIn, StageOut

router = APIRouter(prefix="/stage", tags=["受管暂存"])

ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


@router.post("/text", response_model=StageOut, status_code=201, summary="投递一段临时文本")
def create_stage(
    payload: StageIn,
    identity: ScanDep,
    db: DbDep,
) -> StageOut:
    row = service.create_text(
        db,
        name=payload.name,
        content=payload.content,
        ttl_minutes=payload.ttlMinutes,
        content_type=payload.contentType,
        created_by=identity.user_id or "",
    )
    return StageOut.from_row(row)


@router.get("/s/{token}/{name}", response_class=PlainTextResponse, summary="按 token 下载（过期即 404）")
def download_stage(token: str, name: str, db: DbDep) -> PlainTextResponse:
    service.purge_expired(db)
    row = service.resolve(db, token=token, name=name)
    if row is None:
        raise ApiError.not_found("暂存不存在或已过期")
    return PlainTextResponse(
        row.content,
        media_type=row.content_type,
        headers={"Content-Disposition": f'attachment; filename="{row.name}"'},
    )


@router.delete("/{stage_id}", summary="提前销毁一条暂存")
def delete_stage(stage_id: str, _identity: ScanDep, db: DbDep) -> dict[str, bool]:
    if not service.delete_row(db, stage_id):
        raise ApiError.not_found("暂存不存在")
    return {"deleted": True}
