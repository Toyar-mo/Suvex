"""待批准目标端点（v1.82）。

| 方法 | 路径 | 鉴权 |
| --- | --- | --- |
| POST | `/scans/{scanId}/pending-scope` | scan |
| GET | `/pending-scope` | read |
| POST | `/pending-scope/{itemId}/decide` | scan |

批准**不会**改变正在跑的扫描：扩 scope 需要新建带授权的扫描。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_read, require_scan
from app.core.db import get_db
from app.scans import scope_service
from app.scans.scope_schemas import DecideIn, PendingScopeIn, PendingScopeOut

router = APIRouter(tags=["扫描授权"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


@router.post(
    "/scans/{scanId}/pending-scope",
    response_model=PendingScopeOut,
    status_code=201,
    summary="登记一个待批准的新目标",
)
def add_pending_scope(
    scanId: str,  # noqa: N803
    payload: PendingScopeIn,
    _identity: ScanDep,
    db: DbDep,
) -> PendingScopeOut:
    row = scope_service.add(
        db, scan_id=scanId, target=payload.target, reason=payload.reason
    )
    return PendingScopeOut.from_row(row)


@router.get("/pending-scope", response_model=list[PendingScopeOut], summary="待批准目标列表")
def list_pending_scope(
    _identity: ReadDep,
    db: DbDep,
    status: Annotated[str | None, Query()] = "pending",
) -> list[PendingScopeOut]:
    return [PendingScopeOut.from_row(row) for row in scope_service.list_items(db, status=status)]


@router.post(
    "/pending-scope/{itemId}/decide",
    response_model=PendingScopeOut,
    summary="批准或拒绝一个待批准目标",
)
def decide_pending_scope(
    itemId: str,  # noqa: N803
    payload: DecideIn,
    identity: ScanDep,
    request: Request,
    db: DbDep,
) -> PendingScopeOut:
    row = scope_service.decide(
        db, itemId, approve=payload.approve, actor=identity.describe()
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action="scope.decide",
        summary=(
            f"{'批准' if payload.approve else '拒绝'}待测目标 {row.target}"
        ),
        target=row.scan_id,
        detail={"pendingScopeId": row.id, "target": row.target, "approve": payload.approve},
    )
    return PendingScopeOut.from_row(row)
