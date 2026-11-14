from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth import service
from app.core.timeutil import now_utc


def test_touch_session_cannot_extend_past_absolute_ttl(db_session: Session) -> None:
    account = service.create_account(db_session, "CorrectHorse9!")
    raw, session = service.create_session(
        db_session, account, ip="127.0.0.1", user_agent="test", ttl_hours=12
    )
    session.created_at = now_utc() - timedelta(hours=11)
    session.last_seen_at = now_utc() - timedelta(minutes=5)
    db_session.commit()

    service.touch_session(db_session, session, ttl_hours=12)

    assert session.expires_at <= session.created_at + timedelta(hours=12)
    assert raw  # pragma: no cover - token issuance smoke
