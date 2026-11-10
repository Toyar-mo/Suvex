"""评测快照端点（v1.81）。

捕获是写操作（落一行），但**不改任何业务数据**；权限用 `scan`，
与"能发起扫描"同一档。读取（列表/对比）是 `read`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read, require_scan
from app.benchmark import service
from app.benchmark.schemas import (
    CaptureIn,
    CompareOut,
    SnapshotListOut,
    SnapshotOut,
)
from app.core.db import get_db
from app.core.errors import ApiError

router = APIRouter(prefix="/benchmark", tags=["评测"])


@router.post(
    "/snapshots",
    response_model=SnapshotOut,
    status_code=201,
    summary="捕获当前质量快照",
)
def capture_snapshot(
    payload: CaptureIn,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> SnapshotOut:
    row = service.capture(db, label=payload.label)
    return SnapshotOut.from_row(row)


@router.get("/snapshots", response_model=SnapshotListOut, summary="列出质量快照")
def list_snapshots(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SnapshotListOut:
    rows = service.list_snapshots(db, limit=limit)
    return SnapshotListOut(items=[SnapshotOut.from_row(row) for row in rows])


@router.get("/compare", response_model=CompareOut, summary="比较两份质量快照")
def compare_snapshots(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    before: Annotated[str, Query(min_length=1)],
    after: Annotated[str, Query(min_length=1)],
) -> CompareOut:
    before_row = service.get_snapshot(db, before)
    after_row = service.get_snapshot(db, after)
    if before_row is None or after_row is None:
        raise ApiError.not_found("快照不存在")
    return CompareOut(**service.compare(before_row, after_row))
