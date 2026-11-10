"""可选 Agent 实体（v1.83）。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agents import service
from app.core.errors import ApiError


def test_builtin_profiles_are_seeded(db_session: Session) -> None:
    service.ensure_builtins(db_session)

    rows = service.list_profiles(db_session)

    assert len(rows) >= 10
    assert {row.key for row in rows} >= {"planner", "worker", "verifier"}
    assert all(row.builtin for row in rows)


def test_create_and_delete_custom_profile(db_session: Session) -> None:
    row = service.create(
        db_session,
        key="recon-lead",
        name="侦察负责人",
        description="只做只读侦察",
        mcp_servers=[],
        skills=["recon"],
        tools=["http_request"],
    )

    assert service.get_profile(db_session, row.id).name == "侦察负责人"
    assert service.delete(db_session, row.id) is True
    assert service.get_profile(db_session, row.id) is None


def test_builtin_cannot_be_deleted_or_updated(db_session: Session) -> None:
    service.ensure_builtins(db_session)
    builtin = service.get_by_key(db_session, "planner")

    with pytest.raises(ApiError):
        service.update(db_session, builtin.id, name="改名")
    with pytest.raises(ApiError):
        service.delete(db_session, builtin.id)
