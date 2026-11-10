"""验证上下文（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.facts.models import ScanFact
from app.findings.context import verification_context
from app.findings.models import Finding
from tests.factories import make_scan


def _finding(db: Session, scan_id: str, *, title: str, cwe: str = "CWE-89") -> Finding:
    row = Finding(
        id=new_id("f"),
        scan_id=scan_id,
        severity="high",
        category="injection",
        title=title,
        target="http://app.test/login",
        cwe=cwe,
        verification="verified",
        status="new",
        discovered_at=now_utc(),
    )
    db.add(row)
    db.commit()
    return row


def _fact(db: Session, scan_id: str) -> None:
    db.add(
        ScanFact(
            id=new_id("fact"),
            scan_id=scan_id,
            target="app.test",
            fact_key="endpoint/login",
            category="endpoint",
            summary="登录接口接受表单",
            detail="",
            confidence="confirmed",
            trust_level="L2",
            recorded_by="worker",
            version=1,
            created_at=now_utc(),
            updated_at=now_utc(),
        )
    )
    db.commit()


def test_context_includes_facts_related_and_checks(db_session: Session) -> None:
    scan = make_scan(db_session, status="completed")
    finding = _finding(db_session, scan.id, title="SQL 注入")
    _finding(db_session, scan.id, title="XSS", cwe="CWE-79")
    _fact(db_session, scan.id)

    out = verification_context(db_session, finding)

    assert out["finding"]["id"] == finding.id
    assert [f["factKey"] for f in out["facts"]] == ["endpoint/login"]
    assert {f["title"] for f in out["relatedFindings"]} == {"XSS"}
    assert out["checks"]["class"] == "sqli"
    assert "不等于" in out["note"] or "不等于" in out["checks"]["general"][0]
