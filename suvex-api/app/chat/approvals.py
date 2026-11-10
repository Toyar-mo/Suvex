"""跨会话的审批记录（v1.83）。

ARTEX 有独立的「审批记录」页；Suvex 的审批字段一直存在，但只能在
单条会话里看到。这里把它们汇成只读列表，便于复核"谁批了什么"。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.models import ChatMessage, ChatSession


def list_approvals(
    db: Session, *, created_by: str, limit: int = 100, status: str | None = None
) -> list[dict]:
    stmt = (
        select(ChatMessage, ChatSession)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .where(
            ChatSession.created_by == created_by,
            ChatMessage.approval_status.is_not(None),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ChatMessage.approval_status == status)
    rows = db.execute(stmt).all()
    return [
        {
            "id": message.id,
            "sessionId": session.id,
            "sessionTitle": session.title,
            "toolName": message.tool_name or "",
            "toolArgs": message.tool_args or {},
            "status": message.approval_status,
            "decidedBy": message.approval_decided_by,
            "decidedAt": message.approval_decided_at.isoformat()
            if message.approval_decided_at
            else None,
            "createdAt": message.created_at.isoformat() if message.created_at else "",
        }
        for message, session in rows
    ]


__all__ = ["list_approvals"]
