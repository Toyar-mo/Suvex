"""聊天人格与 Agent 实体同源（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents import service


def test_persona_catalog_reads_names_from_agent_profiles(db_session: Session) -> None:
    service.ensure_builtins(db_session)
    row = service.get_by_key(db_session, "pentest")
    row.name = "自定义侦察"
    db_session.commit()

    catalog = service.persona_catalog(db_session)

    assert next(item for item in catalog if item["id"] == "pentest")["label"] == "自定义侦察"


def test_persona_catalog_keeps_chat_order_and_count(db_session: Session) -> None:
    from app.chat.prompts import CHAT_PERSONAS_SPEC

    catalog = service.persona_catalog(db_session)

    assert [item["id"] for item in catalog] == [spec.id for spec in CHAT_PERSONAS_SPEC]
