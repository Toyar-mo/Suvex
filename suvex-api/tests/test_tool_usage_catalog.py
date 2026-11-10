"""工具清单里的调用统计（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.kernel import service
from tests.factories import make_agent, make_event, make_scan


def test_tools_catalog_includes_calls_and_last_call(db_session: Session) -> None:
    scan = make_scan(db_session)
    agent = make_agent(db_session, scan.id)
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="tool_call",
        summary="调用 run_shell",
        detail={"kind": "tool_call", "tool": "run_shell"},
    )

    payload = service.tools_catalog(db_session)
    tool = next(item for item in payload["tools"] if item["name"] == "run_shell")

    assert tool["calls"] == 1
    assert tool["last_call_at"]
    assert payload["usage_sampled"] == 1
    assert payload["usage_truncated"] is False
