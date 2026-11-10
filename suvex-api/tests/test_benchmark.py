"""评测快照与对比的测试（v1.81）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.benchmark.models import QualitySnapshot
from app.benchmark.service import capture, compare, list_snapshots


def _row(snapshot_id: str, payload: dict) -> QualitySnapshot:
    return QualitySnapshot(
        id=snapshot_id,
        label=snapshot_id,
        payload=payload,
        captured_at=datetime(2026, 11, 10, tzinfo=UTC),
    )


def test_compare_computes_metric_deltas() -> None:
    before = _row(
        "before",
        {
            "challenges": {"solveRate": 0.25},
            "findings": {"total": 10, "verified": 4, "falsePositiveRate": 0.2},
            "coverage": {"surfaces": 2},
            "scans": {"avgDurationSec": 100},
        },
    )
    after = _row(
        "after",
        {
            "challenges": {"solveRate": 0.5},
            "findings": {"total": 10, "verified": 5, "falsePositiveRate": 0.1},
            "coverage": {"surfaces": 4},
            "scans": {"avgDurationSec": 80},
        },
    )

    delta = compare(before, after)["delta"]

    assert delta["solveRate"] == 0.25
    assert delta["falsePositiveRate"] == -0.1
    assert delta["verifiedShare"] == 0.1
    assert delta["coverageSurfaces"] == 2
    assert delta["avgScanDurationSec"] == -20.0


def test_capture_is_listed_newest_first(db_session: Session) -> None:
    first = capture(db_session, label="基线")
    second = capture(db_session, label="换模型后")

    rows = list_snapshots(db_session)

    assert rows[0].id == second.id
    assert {first.id, second.id} <= {row.id for row in rows}
    assert "challenges" in rows[0].payload and "findings" in rows[0].payload


def test_two_snapshots_of_same_state_compare_to_zero(db_session: Session) -> None:
    first = capture(db_session, label="a")
    second = capture(db_session, label="b")

    delta = compare(first, second)["delta"]

    assert all(value in (0, None) for value in delta.values())
