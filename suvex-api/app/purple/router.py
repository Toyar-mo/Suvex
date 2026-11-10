"""紫队：检测规则与 SIEM 导出（v1.82）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read, require_scan
from app.core.db import get_db
from app.core.errors import ApiError
from app.findings.models import Finding
from app.purple import export as export_mod
from app.purple import service
from app.purple.schemas import EvaluateOut, PurpleHit, RuleIn, RuleOut

router = APIRouter(prefix="/purple", tags=["紫队"])


@router.get("/rules", response_model=list[RuleOut], summary="检测规则列表")
def list_rules(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    enabledOnly: Annotated[bool, Query()] = False,
) -> list[RuleOut]:
    return [RuleOut.from_row(row) for row in service.list_rules(db, enabled_only=enabledOnly)]


@router.post("/rules", response_model=RuleOut, status_code=201, summary="新建检测规则")
def create_rule(
    payload: RuleIn,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> RuleOut:
    row = service.create_rule(
        db,
        name=payload.name,
        severity=payload.severity,
        cwe=payload.cwe,
        title_contains=payload.titleContains,
        target_contains=payload.targetContains,
        enabled=payload.enabled,
    )
    return RuleOut.from_row(row)


@router.delete("/rules/{rule_id}", summary="删除检测规则")
def delete_rule(
    rule_id: str,
    _identity: Annotated[Identity, Depends(require_scan)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    if not service.delete_rule(db, rule_id):
        raise ApiError.not_found("规则不存在")
    return {"deleted": True}


@router.post("/evaluate", response_model=EvaluateOut, summary="用规则扫一遍现有发现")
def evaluate(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
) -> EvaluateOut:
    total = int(
        db.scalar(
            select(func.count()).select_from(Finding).where(Finding.deleted_at.is_(None))
        )
        or 0
    )
    return EvaluateOut(
        totalFindings=total,
        hits=[
            PurpleHit(
                rule=RuleOut.from_row(hit["rule"]),
                count=hit["count"],
                samples=hit["samples"],
            )
            for hit in service.evaluate(db)
        ],
    )


@router.get("/export", summary="导出命中为 SIEM 格式（jsonl / cef）")
def export(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    format: Annotated[
        str, Query(pattern="^(jsonl|cef|attack|misp|sigma|suricata)$")
    ] = "jsonl",
) -> PlainTextResponse:
    if format in ("jsonl", "cef"):
        body = service.export_events(db, format)
    else:
        body = export_mod.export_body(
            db, service.list_rules(db, enabled_only=True), format
        )
    media = {
        "jsonl": "application/x-ndjson",
        "attack": "application/json",
        "misp": "application/json",
    }.get(format, "text/plain")
    return PlainTextResponse(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="purple-hits.{format}"'},
    )
