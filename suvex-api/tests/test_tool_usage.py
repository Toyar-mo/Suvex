"""工具调用统计（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.kernel.tool_usage import collect_tool_usage
from tests.factories import make_agent, make_event, make_scan


def _call(db: Session, scan_id: str, agent_id: str, tool: str) -> None:
    make_event(
        db,
        scan_id=scan_id,
        agent_id=agent_id,
        type="tool_call",
        summary=f"调用 {tool}",
        detail={"kind": "tool_call", "tool": tool},
    )


def test_counts_calls_per_tool(db_session: Session) -> None:
    scan = make_scan(db_session)
    agent = make_agent(db_session, scan.id)
    _call(db_session, scan.id, agent.id, "run_shell")
    _call(db_session, scan.id, agent.id, "run_shell")
    _call(db_session, scan.id, agent.id, "http_request")

    report = collect_tool_usage(db_session)

    assert report.rows["run_shell"]["calls"] == 2
    assert report.rows["http_request"]["calls"] == 1
    assert report.sampled == 3
    assert report.truncated is False
    assert report.rows["run_shell"]["lastCallAt"]


def test_ignores_events_without_tool_name(db_session: Session) -> None:
    scan = make_scan(db_session)
    agent = make_agent(db_session, scan.id)
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="tool_call",
        summary="没有工具名",
        detail={"kind": "tool_call"},
    )

    report = collect_tool_usage(db_session)

    assert report.rows == {}
    assert report.sampled == 1


def test_truncation_is_reported(db_session: Session) -> None:
    scan = make_scan(db_session)
    agent = make_agent(db_session, scan.id)
    for _ in range(3):
        _call(db_session, scan.id, agent.id, "run_shell")

    report = collect_tool_usage(db_session, limit=2)

    assert report.truncated is True
    assert report.sampled == 2
