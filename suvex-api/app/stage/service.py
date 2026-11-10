"""受管暂存服务（v1.83）。"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.stage.models import ManagedStage

MAX_TTL_MINUTES = 120
DEFAULT_TTL_MINUTES = 15


def _validate_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise ApiError.validation("name 不能为空")
    if "/" in value or "\\" in value or ".." in value:
        raise ApiError.validation("name 只能是文件名，不能包含路径")
    return value[:255]


def create_text(
    db: Session,
    *,
    name: str,
    content: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    content_type: str = "text/plain",
    created_by: str = "",
) -> ManagedStage:
    ttl = max(1, min(int(ttl_minutes or DEFAULT_TTL_MINUTES), MAX_TTL_MINUTES))
    row = ManagedStage(
        id=new_id("stage"),
        token=secrets.token_urlsafe(24),
        name=_validate_name(name),
        content=content,
        content_type=(content_type or "text/plain")[:120],
        size=len(content.encode("utf-8")),
        created_by=created_by,
        expires_at=now_utc() + timedelta(minutes=ttl),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_active(db: Session, *, created_by: str, limit: int = 100) -> list[ManagedStage]:
    return list(
        db.execute(
            select(ManagedStage)
            .where(
                ManagedStage.created_by == created_by,
                ManagedStage.expires_at > now_utc(),
            )
            .order_by(ManagedStage.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def resolve(db: Session, *, token: str, name: str) -> ManagedStage | None:
    row = db.execute(
        select(ManagedStage).where(
            ManagedStage.token == token, ManagedStage.name == name
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at <= now_utc():
        return None
    if row.downloaded_at is None:
        row.downloaded_at = now_utc()
        db.commit()
    return row


def purge_expired(db: Session) -> int:
    result = db.execute(delete(ManagedStage).where(ManagedStage.expires_at <= now_utc()))
    db.commit()
    return int(result.rowcount or 0)


def delete_row(db: Session, stage_id: str) -> bool:
    row = db.get(ManagedStage, stage_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
