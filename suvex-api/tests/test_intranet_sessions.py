"""内网会话台账（v1.83）。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.intranet import service


def test_create_masks_secret(db_session: Session) -> None:
    row = service.create(
        db_session,
        kind="ssh",
        host="10.0.0.5",
        username="root",
        secret="pw",
        notes="立足点",
        created_by="u1",
    )

    assert row.secret_token != "pw"
    assert service.list_sessions(db_session, created_by="u1")[0].id == row.id


def test_update_status_and_delete(db_session: Session) -> None:
    row = service.create(
        db_session, kind="webshell", host="http://x/shell.php", username="", secret="k"
    )

    updated = service.update(db_session, row.id, status="dead", notes="已失效")
    assert updated.status == "dead"

    assert service.delete(db_session, row.id) is True
    assert service.delete(db_session, row.id) is False


def test_unknown_kind_rejected(db_session: Session) -> None:
    with pytest.raises(ApiError):
        service.create(db_session, kind="telnet", host="x", username="", secret="k")
