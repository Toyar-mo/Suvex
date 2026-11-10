"""跨会话审批记录（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.chat import approvals
from app.chat.models import ChatMessage, ChatSession
from app.core.ids import new_id
from app.core.timeutil import now_utc


def _seed(db: Session, *, owner: str = "u1") -> ChatMessage:
    session = ChatSession(id=new_id("conv"), title="测试会话", created_by=owner)
    db.add(session)
    db.flush()
    message = ChatMessage(
        id=new_id("msg"),
        session_id=session.id,
        seq=1,
        role="tool",
        content="等待确认",
        tool_name="create_scan",
        tool_args={"target": "http://x"},
        approval_status="approved",
        approval_decided_by=owner,
        approval_decided_at=now_utc(),
        created_at=now_utc(),
    )
    db.add(message)
    db.commit()
    return message


def test_lists_only_owned_approvals(db_session: Session) -> None:
    _seed(db_session, owner="u1")
    _seed(db_session, owner="u2")

    rows = approvals.list_approvals(db_session, created_by="u1")

    assert len(rows) == 1
    assert rows[0]["toolName"] == "create_scan"
    assert rows[0]["status"] == "approved"
    assert rows[0]["sessionTitle"] == "测试会话"
