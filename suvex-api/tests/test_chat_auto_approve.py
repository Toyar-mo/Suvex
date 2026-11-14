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


def test_auto_approve_also_covers_add_mcp_server(db_session: Session) -> None:
    """★ 自动放行下拉对 add_mcp_server 也必须生效。

    它平时是“永远强制确认”的工具；但用户显式选了自动放行窗口后，
    仍然每次弹确认，等于那个下拉对这条工具是坏的。
    """
    from datetime import timedelta

    from app.chat import engine
    from app.core.timeutil import now_utc

    session = _session(db_session)
    assert engine._must_approve(session, "add_mcp_server", gate_on=False) is True
    session.auto_approve_until = now_utc() + timedelta(hours=1)
    assert engine._must_approve(session, "add_mcp_server", gate_on=False) is False
