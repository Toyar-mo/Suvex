"""覆盖度聚合的测试（v1.81）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.assets import service
from app.core.ids import new_id
from app.facts.models import ScanFact
from app.facts.service import normalize_target


def _add(
    db: Session,
    *,
    target: str,
    fact_key: str,
    category: str,
    summary: str,
    recorded_by: str = "agent",
) -> None:
    db.add(
        ScanFact(
            id=new_id("fact"),
            scan_id=None,
            fact_key=fact_key,
            category=category,
            summary=summary,
            confidence="tentative",
            recorded_by=recorded_by,
            target=normalize_target(target),
        )
    )
    db.flush()


def test_coverage_groups_surface_and_ruled_out(db_session: Session) -> None:
    target = "http://10.0.0.1:8080/"
    _add(
        db_session,
        target=target,
        fact_key="surface/get-api-users",
        category="surface",
        summary="401 无鉴权",
        recorded_by="root",
    )
    _add(
        db_session,
        target=target,
        fact_key="ruled-out/settings-token-mock",
        category="ruled-out",
        summary="token 是 mock",
    )
    # endpoint 类不在覆盖度快照里（它是"已知接口"，不是"测过的面"）
    _add(
        db_session,
        target=target,
        fact_key="endpoint/login",
        category="endpoint",
        summary="存在",
    )
    db_session.commit()

    out = service.coverage_for_target(db_session, target=target)

    assert out["target"] == normalize_target(target)
    assert [item["factKey"] for item in out["tested"]] == ["surface/get-api-users"]
    assert [item["factKey"] for item in out["ruledOut"]] == [
        "ruled-out/settings-token-mock"
    ]
    assert out["stats"] == {"tested": 1, "ruledOut": 1}
    assert out["truncated"] is False
    assert out["tested"][0]["recordedBy"] == "root"


def test_coverage_is_scoped_to_one_target(db_session: Session) -> None:
    _add(
        db_session,
        target="http://a.test/",
        fact_key="surface/a",
        category="surface",
        summary="a",
    )
    _add(
        db_session,
        target="http://b.test/",
        fact_key="surface/b",
        category="surface",
        summary="b",
    )
    db_session.commit()

    out = service.coverage_for_target(db_session, target="http://a.test")

    assert [item["factKey"] for item in out["tested"]] == ["surface/a"]


def test_coverage_empty_target_returns_empty(db_session: Session) -> None:
    out = service.coverage_for_target(db_session, target="   ")

    assert out["target"] == ""
    assert out["tested"] == []
    assert out["stats"] == {"tested": 0, "ruledOut": 0}


def test_coverage_reports_truncation(db_session: Session) -> None:
    for index in range(3):
        _add(
            db_session,
            target="http://c.test/",
            fact_key=f"surface/s{index}",
            category="surface",
            summary=str(index),
        )
    db_session.commit()

    out = service.coverage_for_target(db_session, target="http://c.test", limit=2)

    assert out["truncated"] is True
    assert len(out["tested"]) == 2
