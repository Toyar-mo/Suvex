"""待批准目标（v1.82）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.scans import scope_service


def test_add_is_idempotent_for_pending_target(db_session: Session) -> None:
    first = scope_service.add(db_session, scan_id="scan_1", target="10.0.0.5")
    second = scope_service.add(db_session, scan_id="scan_1", target="10.0.0.5")

    assert first.id == second.id


def test_decide_approves_and_records_actor(db_session: Session) -> None:
    item = scope_service.add(db_session, scan_id="scan_1", target="10.0.0.6")

    decided = scope_service.decide(db_session, item.id, approve=True, actor="管理员会话")

    assert decided.status == "approved"
    assert decided.decided_by == "管理员会话"
    assert decided.decided_at is not None


def test_deciding_twice_is_rejected(db_session: Session) -> None:
    item = scope_service.add(db_session, scan_id="scan_1", target="10.0.0.7")
    scope_service.decide(db_session, item.id, approve=False, actor="a")

    with pytest.raises(ApiError):
        scope_service.decide(db_session, item.id, approve=True, actor="b")


def test_list_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/pending-scope").status_code in (401, 403)
