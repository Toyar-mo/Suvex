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


# -- 评测用例(v1.81) ------------------------------------------


def _scan_case(db_session: Session, *, status: str, minimum: int = 0):
    from app.benchmark.service import create_case
    from tests.factories import make_scan

    scan = make_scan(db_session, status=status)
    return create_case(
        db_session,
        suite="smoke",
        name=f"scan-{status}",
        kind="scan",
        scan_id=scan.id,
        expect={"minFindings": minimum},
    )


def test_scan_case_completed_without_minimum_passes(db_session: Session) -> None:
    from app.benchmark.service import run_case

    case = _scan_case(db_session, status="completed")

    run = run_case(db_session, case)

    assert run.status == "passed"
    assert run.detail["findings"] == 0


def test_scan_case_completed_with_minimum_fails_when_no_findings(db_session: Session) -> None:
    from app.benchmark.service import run_case

    case = _scan_case(db_session, status="completed", minimum=1)

    assert run_case(db_session, case).status == "failed"


def test_scan_case_running_is_pending(db_session: Session) -> None:
    from app.benchmark.service import run_case

    case = _scan_case(db_session, status="running")

    assert run_case(db_session, case).status == "pending"


def test_ctf_case_solved_passes(db_session: Session) -> None:
    from app.benchmark.service import create_case, run_case
    from app.core.ids import new_id
    from app.core.timeutil import now_utc
    from app.kernel.models import KernelChallenge, KernelCompetition

    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=None,
        remote_id=new_id("local"),
        kind="local",
        name="评测用例",
        remote_status="ongoing",
        challenge_count=1,
        synced_at=now_utc(),
    )
    db_session.add(competition)
    db_session.flush()
    challenge = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition.id,
        remote_id="9001",
        stage_id=None,
        title="示例题",
        category="crypto",
        description=None,
        attachments=[],
        score=100,
        status="solved",
        candidate_flag=None,
        confirmed_flag="flag{x}",
        wrong_submission_count=0,
        max_wrong_submissions=3,
        candidate_count=1,
        sandbox_profile="suvex-sandbox",
    )
    db_session.add(challenge)
    db_session.commit()

    case = create_case(
        db_session,
        suite="smoke",
        name="ctf-solved",
        kind="ctf",
        challenge_id=challenge.id,
    )

    run = run_case(db_session, case)

    assert run.status == "passed"
    assert run.detail["challengeStatus"] == "solved"


def test_run_suite_counts_and_delete(db_session: Session) -> None:
    from app.benchmark import service

    case = _scan_case(db_session, status="completed")

    out = service.run_suite(db_session, "smoke")

    assert len(out) == 1
    assert out[0].case_id == case.id
    assert service.delete_case(db_session, case.id) is True
    assert service.delete_case(db_session, case.id) is False
