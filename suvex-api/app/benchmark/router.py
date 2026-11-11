"""评测快照端点（v1.81）。

捕获是写操作（落一行），但**不改任何业务数据**；权限用 `scan`，
与"能发起扫描"同一档。读取（列表/对比）是 `read`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read, require_scan
from app.benchmark import service
from app.benchmark.schemas import (
    CaptureIn,
    CaseIn,
    CaseOut,
    CompareOut,
    LaunchOut,
    RunOut,
    SnapshotListOut,
    SnapshotOut,
    SuiteRunOut,
    SummaryOut,
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


# -- 评测用例(v1.81) ------------------------------------------


@router.post("/cases", response_model=CaseOut, status_code=201, summary="登记评测用例")
def create_case(
    payload: CaseIn,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> CaseOut:
    row = service.create_case(
        db,
        suite=payload.suite,
        name=payload.name,
        kind=payload.kind,
        challenge_id=payload.challengeId,
        scan_id=payload.scanId,
        expect=payload.expect,
        enabled=payload.enabled,
    )
    return CaseOut.from_row(row)


@router.get("/cases", response_model=list[CaseOut], summary="列出评测用例")
def list_cases(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    suite: Annotated[str | None, Query(max_length=80)] = None,
) -> list[CaseOut]:
    return [CaseOut.from_row(row) for row in service.list_cases(db, suite=suite)]


@router.delete("/cases/{case_id}", summary="删除评测用例")
def delete_case(
    case_id: str,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    if not service.delete_case(db, case_id):
        raise ApiError.not_found("用例不存在")
    return {"deleted": True}


@router.post("/suites/{suite}/run", response_model=SuiteRunOut, summary="采集一套用例的结果")
def run_suite(
    suite: str,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> SuiteRunOut:
    """只读关联对象判定，不触发扫描/解题。"""
    runs = [RunOut.from_row(row) for row in service.run_suite(db, suite)]
    return SuiteRunOut(
        suite=suite,
        runs=runs,
        passed=sum(1 for run in runs if run.status == "passed"),
        failed=sum(1 for run in runs if run.status == "failed"),
        pending=sum(1 for run in runs if run.status == "pending"),
        error=sum(1 for run in runs if run.status == "error"),
    )


@router.get("/runs", response_model=list[RunOut], summary="最近一次评测结果")
def latest_runs(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    suite: Annotated[str | None, Query(max_length=80)] = None,
) -> list[RunOut]:
    return [RunOut.from_row(row) for row in service.latest_runs(db, suite=suite)]


@router.post(
    "/cases/{case_id}/launch",
    response_model=LaunchOut,
    status_code=201,
    summary="主动执行一次用例（真的建扫描）",
)
def launch_case(
    case_id: str,
    request: Request,
    identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> LaunchOut:
    """登记了 target 的 scan 用例：建一次扫描，并立即写一条 `pending` 判定。

    跑完之后调 `POST /suites/{suite}/run` 重新判定（只读），或直接看 `/summary`。
    """
    case = service.get_case(db, case_id)
    if case is None:
        raise ApiError.not_found("用例不存在")
    scan, run = service.launch_case(
        db,
        case=case,
        identity=identity,
        ip=request.client.host if request.client else "benchmark",
        user_agent=request.headers.get("user-agent"),
    )
    return LaunchOut(caseId=case.id, scanId=scan.id, status=run.status)


@router.get("/summary", response_model=SummaryOut, summary="一套用例的成绩汇总")
def summary(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    suite: Annotated[str | None, Query(max_length=80)] = None,
) -> SummaryOut:
    return SummaryOut(**service.summary(db, suite=suite))
