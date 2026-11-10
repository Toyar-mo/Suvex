"""模型路由与设置读写的测试（v1.81）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.providers.routing import route_for_goal
from app.settings import service
from app.settings.models import SINGLETON_ID, ModelRoutes


def test_explicit_provider_wins_without_touching_routes() -> None:
    assert route_for_goal(None, "findVulns", "provider-explicit") == "provider-explicit"  # type: ignore[arg-type]


def test_auto_uses_configured_route(db_session: Session) -> None:
    db_session.add(
        ModelRoutes(id=SINGLETON_ID, routes={"captureFlag": "provider-ctf"})
    )
    db_session.commit()

    assert route_for_goal(db_session, "captureFlag", "auto") == "provider-ctf"


def test_unconfigured_goal_falls_back_to_default(db_session: Session) -> None:
    # 没有路由、没有默认 provider → None（调用方报"先配置模型"）。
    assert route_for_goal(db_session, "findVulns", "auto") is None


def test_settings_roundtrip_drops_empty_entries(db_session: Session) -> None:
    service.write_model_routes(
        db_session,
        routes={"findVulns": "p1", "captureFlag": "", " ": "x"},
    )

    row = service.read_model_routes(db_session)

    assert row.routes == {"findVulns": "p1"}


def test_skill_route_used_when_goal_unconfigured(db_session: Session) -> None:
    db_session.add(ModelRoutes(id=SINGLETON_ID, routes={"skill:crypto": "provider-crypto"}))
    db_session.commit()

    assert (
        route_for_goal(db_session, "findVulns", "auto", skills=["web", "crypto"])
        == "provider-crypto"
    )


def test_goal_route_beats_skill_route(db_session: Session) -> None:
    db_session.add(
        ModelRoutes(
            id=SINGLETON_ID,
            routes={"findVulns": "provider-goal", "skill:web": "provider-web"},
        )
    )
    db_session.commit()

    assert (
        route_for_goal(db_session, "findVulns", "auto", skills=["web"]) == "provider-goal"
    )
