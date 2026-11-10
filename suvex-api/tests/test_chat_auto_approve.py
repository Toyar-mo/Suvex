"""审批自动放行（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.chat import service
from app.chat.models import ChatSession
from app.chat.schemas import ChatSettingsIn
from app.core.ids import new_id


def _session(db: Session) -> ChatSession:
    row = ChatSession(id=new_id("conv"), title="t", created_by="u1")
    db.add(row)
    db.commit()
    return row


def test_auto_approve_sets_and_clears_deadline(db_session: Session) -> None:
    session = _session(db_session)

    service.update_settings(
        db_session, session=session, payload=ChatSettingsIn(autoApproveHours=2)
    )
    assert session.auto_approve_until is not None

    service.update_settings(
        db_session, session=session, payload=ChatSettingsIn(autoApproveHours=0)
    )
    assert session.auto_approve_until is None
