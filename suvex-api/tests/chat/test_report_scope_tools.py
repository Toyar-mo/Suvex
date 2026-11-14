"""复测/验证之外的最后一环：报告落库与内网扩面审批。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import service as chat_service
from app.findings import auto_verify
from app.reports.models import Report
from app.scans import scope_service
from app.scans.models import Scan
from tests.test_chat import ADMIN


def _ctx(db: Session, *, agent_task_id: str | None = None):  # noqa: ANN202
    session = chat_service.create_session(db, created_by="user_admin")
    session.agent_task_id = agent_task_id
    db.commit()
    return engine._ToolContext(db=db, session=session, identity=ADMIN)


async def test_save_report_tool_writes_a_frozen_report(db_session: Session, imported) -> None:
    finding = imported[0]
    ctx = _ctx(db_session)

    out, failed = await engine._execute_tool(
        ctx,
        "save_report",
        {
            "title": "自动生成的验证报告",
            "finding_ids": [finding.id],
            "only_confirmed": False,
        },
    )

    assert not failed, out
    report = db_session.execute(select(Report)).scalar_one()
    assert report.finding_count == 1
    assert report.id in out and report.code in out


async def test_request_scope_approval_registers_pending_target(
    db_session: Session, scan: Scan
) -> None:
    ctx = _ctx(db_session, agent_task_id=scan.agent_task_id)

    out, failed = await engine._execute_tool(
        ctx,
        "request_scope_approval",
        {"target": "10.20.30.0/24", "reason": "内网横向前先登记", "scan_id": scan.id},
    )

    assert not failed, out
    row = db_session.execute(select(scope_service.PendingScope)).scalar_one()
    assert row.scan_id == scan.id
    assert row.target == "10.20.30.0/24"
    assert row.status == "pending"


def test_auto_verify_schedules_a_verifier_task(
    db_session: Session, imported, scan: Scan, monkeypatch
) -> None:
    finding = imported[0]
    monkeypatch.setattr(auto_verify, "_run", lambda *args: None)

    task_ids = auto_verify.schedule(db_session, scan, (finding.id,), limit=1)

    assert len(task_ids) == 1
    from app.agents.graph_models import AgentTask

    task = db_session.get(AgentTask, task_ids[0])
    assert task is not None
    assert finding.id in task.objective


def test_auto_verify_skips_findings_without_evidence(
    db_session: Session, scan: Scan
) -> None:
    from app.findings.models import Finding

    finding = Finding(
        id="finding_no_evidence",
        scan_id=scan.id,
        severity="low",
        category="info_disclosure",
        title="无证据",
        target=scan.target,
        status="new",
    )
    db_session.add(finding)
    db_session.commit()
    db_session.refresh(finding)

    assert auto_verify.schedule(db_session, scan, (finding.id,), limit=1) == []
