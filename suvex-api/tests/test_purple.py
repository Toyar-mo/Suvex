"""紫队规则与导出（v1.82）。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.findings.models import Finding
from app.purple import service
from tests.factories import make_scan


def _finding(
    db: Session,
    *,
    scan_id: str,
    cwe: str | None,
    title: str,
    target: str = "http://app.test/login",
) -> Finding:
    row = Finding(
        id=new_id("f"),
        scan_id=scan_id,
        severity="high",
        category="injection",
        title=title,
        target=target,
        cwe=cwe,
        verification="verified",
        status="new",
        discovered_at=now_utc(),
    )
    db.add(row)
    db.commit()
    return row


def test_evaluate_matches_by_cwe_and_skips_unconstrained_rules(db_session: Session) -> None:
    scan = make_scan(db_session, status="completed")
    _finding(db_session, scan_id=scan.id, cwe="CWE-89", title="SQL 注入")
    _finding(db_session, scan_id=scan.id, cwe="CWE-79", title="反射型 XSS")

    service.create_rule(db_session, name="SQLi", cwe="CWE-89", severity="high")
    service.create_rule(db_session, name="无约束规则")

    hits = service.evaluate(db_session)

    assert [hit["rule"].name for hit in hits] == ["SQLi"]
    assert hits[0]["count"] == 1
    assert hits[0]["samples"][0]["cwe"] == "CWE-89"


def test_matches_is_case_insensitive_on_title(db_session: Session) -> None:
    scan = make_scan(db_session, status="completed")
    _finding(db_session, scan_id=scan.id, cwe=None, title="Reflected XSS in /search")
    rule = service.create_rule(db_session, name="xss-rule", title_contains="reflected xss")

    hits = service.evaluate(db_session)

    assert hits[0]["rule"].id == rule.id


def test_export_jsonl_and_cef(db_session: Session) -> None:
    scan = make_scan(db_session, status="completed")
    _finding(db_session, scan_id=scan.id, cwe="CWE-89", title="SQL 注入")
    service.create_rule(db_session, name="SQLi|规则", cwe="CWE-89", severity="high")

    jsonl = service.export_events(db_session, "jsonl")
    cef = service.export_events(db_session, "cef")

    payload = json.loads(jsonl.strip())
    assert payload["rule"] == "SQLi|规则"
    assert payload["count"] == 1
    assert cef.startswith("CEF:0|Suvex|Purple|1.82|")
    # 规则名里的 `|` 必须转义，否则 CEF 字段被撕开。
    assert "SQLi\\|规则" in cef


def test_rules_crud(db_session: Session) -> None:
    rule = service.create_rule(db_session, name="SSRF", cwe="CWE-918")

    assert service.list_rules(db_session)[0].id == rule.id
    assert service.delete_rule(db_session, rule.id) is True
    assert service.delete_rule(db_session, rule.id) is False
