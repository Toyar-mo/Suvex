"""Agent 绑定到聊天工具集（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents import service


def test_empty_binding_means_unrestricted(db_session: Session) -> None:
    service.ensure_builtins(db_session)

    assert service.allowed_tools_for_persona(db_session, "recon") is None


def test_binding_filters_to_known_chat_tools(db_session: Session) -> None:
    service.create(
        db_session,
        key="locked",
        name="限定",
        description="",
        tools=["list_scans", "not_a_chat_tool"],
    )

    assert service.allowed_tools_for_persona(db_session, "locked") == {"list_scans"}


def test_unknown_persona_is_unrestricted(db_session: Session) -> None:
    assert service.allowed_tools_for_persona(db_session, "missing") is None
