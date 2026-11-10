"""质量指标快照的测试（v1.81）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.facts.models import ScanFact
from app.facts.service import normalize_target
from app.findings.models import Finding
from app.overview.quality import quality_snapshot
from app.scans.models import Scan


def test_empty_database_returns_nulls_not_zero_rates(db_session: Session) -> None:
    out = quality_snapshot(db_session)

    assert out["scans"]["total"] == 0
    assert out["scans"]["avgDurationSec"] is None
    assert out["findings"]["total"] == 0
    assert out["findings"]["falsePositiveRate"] is None
    assert out["challenges"]["solveRate"] is None
    assert out["coverage"] == {"surfaces": 0, "targets": 0}


def test_counts_scans_findings_and_coverage(db_session: Session) -> None:
    at = now_utc()
    db_session.add(
        Scan(
            id="scan_q1",
            name="q.test 评估",
            target="http://q.test",
            target_kind="url",
            goal="findVulns",
            status="completed",
            provider_id="provider",
            started_at=at,
            finished_at=at,
            duration_sec=120,
        )
    )
    db_session.flush()  # Scan 必须先落库，finding 的 FK 才能过
    db_session.add(
        Finding(
            id="finding_q1",
            scan_id="scan_q1",
            severity="high",
            category="broken_access_control",
            title="越权",
            target="http://q.test/api",
            verification="verified",
            status="new",
        )
    )
    db_session.add(
        Finding(
            id="finding_q2",
            scan_id="scan_q1",
            severity="low",
            category="broken_access_control",
            title="误报",
            target="http://q.test/x",
            verification="none",
            status="false_positive",
        )
    )
    db_session.add(
        ScanFact(
            id="fact_q1",
            scan_id=None,
            fact_key="surface/api",
            category="surface",
            summary="401 无鉴权",
            confidence="tentative",
            target=normalize_target("http://q.test"),
        )
    )
    db_session.commit()

    out = quality_snapshot(db_session)

    assert out["scans"] == {
        "total": 1,
        "completed": 1,
        "failed": 0,
        "aborted": 0,
        "avgDurationSec": 120.0,
    }
    assert out["findings"]["total"] == 2
    assert out["findings"]["verified"] == 1
    assert out["findings"]["none"] == 1
    assert out["findings"]["falsePositive"] == 1
    assert out["findings"]["falsePositiveRate"] == 0.5
    assert out["coverage"] == {"surfaces": 1, "targets": 1}
