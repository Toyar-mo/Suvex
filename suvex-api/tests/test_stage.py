"""受管暂存（v1.83）。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.stage import service


def test_create_and_resolve(db_session: Session) -> None:
    row = service.create_text(
        db_session, name="tool.sh", content="echo hi\n", ttl_minutes=15, created_by="u1"
    )

    resolved = service.resolve(db_session, token=row.token, name="tool.sh")

    assert resolved is not None
    assert resolved.content == "echo hi\n"
    assert row.token not in ("", None)


def test_expired_is_not_resolvable(db_session: Session) -> None:
    row = service.create_text(
        db_session, name="a.txt", content="x", ttl_minutes=15, created_by="u1"
    )
    row.expires_at = now_utc() - timedelta(minutes=1)
    db_session.commit()

    assert service.resolve(db_session, token=row.token, name="a.txt") is None


def test_purge_removes_expired(db_session: Session) -> None:
    row = service.create_text(
        db_session, name="a.txt", content="x", ttl_minutes=15, created_by="u1"
    )
    row.expires_at = now_utc() - timedelta(minutes=1)
    db_session.commit()

    assert service.purge_expired(db_session) == 1
    assert service.resolve(db_session, token=row.token, name="a.txt") is None


def test_list_active_excludes_expired_and_other_users(db_session: Session) -> None:
    live = service.create_text(
        db_session, name="live.sh", content="x", ttl_minutes=15, created_by="u1"
    )
    expired = service.create_text(
        db_session, name="old.sh", content="x", ttl_minutes=15, created_by="u1"
    )
    expired.expires_at = now_utc() - timedelta(minutes=1)
    db_session.commit()
    service.create_text(
        db_session, name="other.sh", content="x", ttl_minutes=15, created_by="u2"
    )

    rows = service.list_active(db_session, created_by="u1")

    assert [row.id for row in rows] == [live.id]
