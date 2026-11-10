"""黑板事实图的测试（v1.81）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.facts.models import ScanFact
from app.facts.service import normalize_target, surface_map


def _add(
    db: Session,
    *,
    target: str,
    fact_key: str,
    category: str,
    summary: str,
    edges: list[dict] | None = None,
) -> None:
    db.add(
        ScanFact(
            id=new_id("fact"),
            scan_id=None,
            fact_key=fact_key,
            category=category,
            summary=summary,
            confidence="tentative",
            target=normalize_target(target),
            edges=edges or [],
        )
    )
    db.flush()


def test_surface_map_keeps_only_edges_between_known_nodes(db_session: Session) -> None:
    target = "http://graph.test/"
    _add(
        db_session,
        target=target,
        fact_key="endpoint/api-users",
        category="endpoint",
        summary="存在",
        edges=[{"type": "enables", "fact_key": "surface/api-users"}],
    )
    _add(
        db_session,
        target=target,
        fact_key="surface/api-users",
        category="surface",
        summary="401 无鉴权",
        edges=[{"type": "requires", "fact_key": "missing/node"}],
    )
    db_session.commit()

    out = surface_map(db_session, target=target)

    assert out["target"] == normalize_target(target)
    assert {node["factKey"] for node in out["nodes"]} == {
        "endpoint/api-users",
        "surface/api-users",
    }
    assert out["edges"] == [
        {"from": "endpoint/api-users", "type": "enables", "to": "surface/api-users"}
    ]
    assert out["truncated"] is False


def test_surface_map_is_scoped_by_target(db_session: Session) -> None:
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

    out = surface_map(db_session, target="http://a.test")

    assert [node["factKey"] for node in out["nodes"]] == ["surface/a"]


def test_surface_map_empty_target(db_session: Session) -> None:
    assert surface_map(db_session, target="  ") == {
        "target": "",
        "nodes": [],
        "edges": [],
        "truncated": False,
    }


def test_surface_map_reports_truncation(db_session: Session) -> None:
    for index in range(3):
        _add(
            db_session,
            target="http://t.test/",
            fact_key=f"note/n{index}",
            category="note",
            summary=str(index),
        )
    db_session.commit()

    out = surface_map(db_session, target="http://t.test", limit=2)

    assert out["truncated"] is True
    assert len(out["nodes"]) == 2
