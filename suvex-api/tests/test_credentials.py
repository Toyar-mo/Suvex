"""凭据台账（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.credentials import service


def test_create_stores_ciphertext_and_reveals(db_session: Session) -> None:
    row = service.create(
        db_session,
        kind="password",
        username="admin",
        secret="P@ssw0rd!",
        target="http://app.test:8080",
        source="manual",
    )

    assert row.secret_token != "P@ssw0rd!"
    assert service.reveal(db_session, row.id) == "P@ssw0rd!"


def test_reuse_plan_prefers_same_target(db_session: Session) -> None:
    service.create(
        db_session,
        kind="password",
        username="admin",
        secret="a",
        target="http://app.test:8080",
    )
    second = service.create(
        db_session,
        kind="password",
        username="root",
        secret="b",
        target="http://app.test:8080/login",
    )

    plan = service.reuse_plan(db_session, second.id)

    assert plan["score"] >= 0.8
    assert any("同一目标" in item for item in plan["basis"])


def test_reuse_plan_is_low_for_unrelated_target(db_session: Session) -> None:
    row = service.create(
        db_session,
        kind="password",
        username="admin",
        secret="x",
        target="http://only.test",
    )

    plan = service.reuse_plan(db_session, row.id)

    assert plan["score"] <= 0.4
    assert plan["basis"]


def test_delete_credential(db_session: Session) -> None:
    row = service.create(
        db_session,
        kind="password",
        username="admin",
        secret="x",
        target="http://a.test",
    )

    assert service.delete(db_session, row.id) is True
    assert service.delete(db_session, row.id) is False
