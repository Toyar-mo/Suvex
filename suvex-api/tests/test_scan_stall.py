"""扫描重复动作检测（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.scans.stall import stalled_scans
from tests.factories import make_agent, make_event, make_scan


def _events(db: Session, scan_id: str, agent_id: str, summary: str, count: int) -> None:
    for _ in range(count):
        make_event(
            db,
            scan_id=scan_id,
            agent_id=agent_id,
            type="tool_call",
            summary=summary,
            detail={"kind": "tool_call", "tool": "run_shell"},
        )


def test_repeated_tool_call_is_stalled(db_session: Session) -> None:
    scan = make_scan(db_session, status="running")
    agent = make_agent(db_session, scan.id)
    _events(db_session, scan.id, agent.id, "curl http://x/login", 5)

    rows = stalled_scans(db_session)

    assert len(rows) == 1
    assert rows[0]["scanId"] == scan.id
    assert rows[0]["repeats"] == 5


def test_varied_calls_are_not_stalled(db_session: Session) -> None:
    scan = make_scan(db_session, status="running")
    agent = make_agent(db_session, scan.id)
    for index in range(5):
        _events(db_session, scan.id, agent.id, f"curl http://x/{index}", 1)

    assert stalled_scans(db_session) == []
