"""内网会话台账服务（v1.83）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.intranet.models import KINDS, STATUSES, IntranetSession


def create(
    db: Session,
    *,
    kind: str,
    host: str,
    username: str = "",
    secret: str,
    notes: str = "",
    status: str = "unknown",
    created_by: str = "",
) -> IntranetSession:
    if kind not in KINDS:
        raise ApiError.validation(f"kind 只能是 {' / '.join(KINDS)}")
    if status not in STATUSES:
        raise ApiError.validation(f"status 只能是 {' / '.join(STATUSES)}")
    if not (host or "").strip():
        raise ApiError.validation("host 不能为空")
    if not (secret or "").strip():
        raise ApiError.validation("secret 不能为空")
    row = IntranetSession(
        id=new_id("isess"),
        kind=kind,
        host=host.strip()[:512],
        username=(username or "").strip()[:200],
        secret_token=encrypt_secret(secret),
        status=status,
        notes=(notes or "").strip()[:1000],
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_sessions(db: Session, *, created_by: str | None = None) -> list[IntranetSession]:
    stmt = select(IntranetSession).order_by(IntranetSession.created_at.desc())
    if created_by:
        stmt = stmt.where(IntranetSession.created_by == created_by)
    return list(db.execute(stmt).scalars())


def update(db: Session, session_id: str, **fields) -> IntranetSession:
    row = db.get(IntranetSession, session_id)
    if row is None:
        raise ApiError.not_found("会话不存在")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ApiError.validation(f"status 只能是 {' / '.join(STATUSES)}")
    for name in ("status", "notes", "username"):
        if name in fields and fields[name] is not None:
            setattr(row, name, str(fields[name])[:1000])
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, session_id: str) -> bool:
    row = db.get(IntranetSession, session_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
