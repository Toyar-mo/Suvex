"""待批准目标（v1.82）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.scans.models import PendingScope

STATUSES = ("pending", "approved", "rejected")


def add(db: Session, *, scan_id: str, target: str, reason: str = "") -> PendingScope:
    """登记一个待批准目标；同一扫描同一目标已 pending 时返回原记录。"""
    value = (target or "").strip()
    if not value:
        raise ApiError.validation("target 不能为空")
    existing = db.execute(
        select(PendingScope).where(
            PendingScope.scan_id == scan_id,
            PendingScope.target == value,
            PendingScope.status == "pending",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = PendingScope(
        id=new_id("scope"),
        scan_id=scan_id,
        target=value[:512],
        reason=(reason or "").strip()[:500],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_items(db: Session, *, status: str | None = None) -> list[PendingScope]:
    stmt = select(PendingScope).order_by(PendingScope.created_at.desc())
    if status:
        if status not in STATUSES:
            raise ApiError.validation(f"status 只能是 {' / '.join(STATUSES)}")
        stmt = stmt.where(PendingScope.status == status)
    return list(db.execute(stmt).scalars())


def decide(db: Session, item_id: str, *, approve: bool, actor: str) -> PendingScope:
    row = db.get(PendingScope, item_id)
    if row is None:
        raise ApiError.not_found("待批准目标不存在")
    if row.status != "pending":
        raise ApiError.validation("该目标已有决定")
    row.status = "approved" if approve else "rejected"
    row.decided_at = now_utc()
    row.decided_by = (actor or "")[:120]
    db.commit()
    db.refresh(row)
    return row
