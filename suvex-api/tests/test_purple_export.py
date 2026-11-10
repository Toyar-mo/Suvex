"""检测对抗导出（v1.82）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.findings.models import Finding
from app.purple.export import (
    CWE_ATTACK,
    build_attack_layer,
    build_misp_event,
    build_sigma_draft,
    build_suricata_draft,
)
from app.purple.models import PurpleRule
from tests.factories import make_scan


def _rule(cwe: str, name: str = "SQLi", severity: str = "high") -> PurpleRule:
    return PurpleRule(
        id=new_id("rule"),
        name=name,
        severity=severity,
        cwe=cwe,
        title_contains=None,
        target_contains=None,
        enabled=True,
        created_at=now_utc(),
    )


def _finding(db: Session, *, target: str) -> Finding:
    scan = make_scan(db, status="completed")
    row = Finding(
        id=new_id("f"),
        scan_id=scan.id,
        severity="high",
        category="injection",
        title="SQL 注入",
        target=target,
        cwe="CWE-89",
        verification="verified",
        status="new",
        discovered_at=now_utc(),
    )
    db.add(row)
    db.commit()
    return row


def test_attack_layer_maps_cwe_to_technique() -> None:
    layer = build_attack_layer([_rule("CWE-89")])

    assert layer["domain"] == "enterprise-attack"
    assert [t["techniqueID"] for t in layer["techniques"]] == CWE_ATTACK["CWE-89"]
    assert "SQLi" in layer["techniques"][0]["comment"]


def test_sigma_draft_is_marked_experimental() -> None:
    sigma = build_sigma_draft(_rule("CWE-89"))

    assert "status: experimental" in sigma
    assert "DRAFT" in sigma
    assert "union select" in sigma


def test_suricata_draft_uses_target_host() -> None:
    finding = type(
        "F",
        (),
        {"title": "SQL 注入", "target": "http://app.test:8080/login"},
    )()

    rule = build_suricata_draft(finding, 1900001)  # type: ignore[arg-type]

    assert "app.test" in rule
    assert "sid:1900001" in rule
    assert rule.startswith("# DRAFT")


def test_misp_event_extracts_url_attributes(db_session: Session) -> None:
    _finding(db_session, target="http://app.test:8080/login")

    event = build_misp_event(db_session)

    attributes = event["Event"]["Attribute"]
    assert attributes[0]["type"] == "url"
    assert attributes[0]["value"] == "http://app.test:8080/login"
    assert attributes[0]["to_ids"] is True
